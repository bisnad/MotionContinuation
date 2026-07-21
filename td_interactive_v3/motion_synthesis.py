import torch
from torch import nn
import torch.nn.functional as nnF
import numpy as np
import math

from common.rotation_utils_numpy import RotationUtilsNumpy as rot_np
from common.rotation_utils_torch import RotationUtilsTorch as rot_to

config = {
    "skeleton": None,
    "rot_sequence": [0, 1, 2],
    "source_mocap_format": "fbx",
    "model": None,
    "seq_length": 64,
    "orig_sequences": [],
    "orig_seq_index": 0,
    "device": "cuda",
    "train_root_trajectory": True,
    "root_pos_mean": None,
    "root_pos_std": None,
    "joint_count": 0,
    "mocap_fps": 50,
    "smooth_cutoff": 0.5,
    "smooth_beta": 0.01,
    "gaussian_temp": 0.1,
    "pi_temperature": 2.0,
    "top_p": 0.9
}

class OneEuroFilterTorch:
    def __init__(self, freq, mincutoff=1.0, beta=0.0, dcutoff=1.0):
        self.freq = freq
        self.mincutoff = mincutoff
        self.beta = beta
        self.dcutoff = dcutoff
        self.x_prev = None
        self.dx_prev = None

    def setMinCutOff(self, mincutoff):
        self.mincutoff = mincutoff

    def setBeta(self, beta):
        self.beta = beta

    def _alpha(self, cutoff):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau * self.freq)

    def __call__(self, x):
        if self.x_prev is None:
            self.x_prev = x.clone()
            self.dx_prev = torch.zeros_like(x)
            return x.clone()

        dx = (x - self.x_prev) * self.freq
        a_dcutoff = self._alpha(self.dcutoff)
        edx = a_dcutoff * dx + (1.0 - a_dcutoff) * self.dx_prev
        cutoff = self.mincutoff + self.beta * torch.abs(edx)
        a = self._alpha(cutoff)
        x_filtered = a * x + (1.0 - a) * self.x_prev

        self.x_prev = x_filtered.clone()
        self.dx_prev = edx.clone()
        return x_filtered

class MotionSynthesis():
    def __init__(self, config):
        self.skeleton = config["skeleton"]
        self.rot_sequence = config.get("rot_sequence", [0, 1, 2])
        self.source_mocap_format = config.get("source_mocap_format", "fbx")
        self.model = config["model"]
        self.seq_length = config["seq_length"]
        self.device = config["device"]
        self.orig_sequences = config["orig_sequences"]
        self.orig_seq_index = config["orig_seq_index"]
        self.orig_seq_start_frame_index = 0
        self.orig_seq_frame_count = self.seq_length
        self.orig_seq_blend_factor = 1.0
        self.seq_rand_range = 0.00
        
        self.train_root_trajectory = config.get("train_root_trajectory", True)
        self.root_pos_mean = config.get("root_pos_mean")
        self.root_pos_std = config.get("root_pos_std")

        self.joint_count = config.get("joint_count", 0)
        self.mocap_fps = config.get("mocap_fps", 0)

        self.motion_seq = np.copy(self.orig_sequences[self.orig_seq_index][self.orig_seq_start_frame_index:self.orig_seq_start_frame_index + self.orig_seq_frame_count, ...])

        if self.train_root_trajectory:
            self.motion_seq[:, :3] = (self.motion_seq[:, :3] - self.root_pos_mean.flatten()) / self.root_pos_std.flatten()

        self.motion_seq = torch.from_numpy(self.motion_seq).to(self.device)
        self.orig_seq_changed = False

        self.input_dim = self.motion_seq.shape[1]

        self.joint_offsets = self.skeleton["offsets"].astype(np.float32)
        self.joint_parents = self.skeleton["parents"]
        self.joint_children = self.skeleton["children"]

        self._create_edge_list()
        self._build_execution_order()

        self.synth_pose_wpos = None
        self.synth_pose_wrot = None
        self.synth_pose_lrot = None

        smooth_freq = self.mocap_fps
        smooth_cutoff = config["smooth_cutoff"]
        smooth_beta = config["smooth_beta"]
        self.one_euro_filter = OneEuroFilterTorch(freq=smooth_freq, mincutoff=smooth_cutoff, beta=smooth_beta)

        self.gaussian_temp = config["gaussian_temp"]
        self.pi_temperature = config["pi_temperature"]
        self.top_p = config["top_p"]

    def _create_edge_list(self):
        self.edge_list = []
        for parent_joint_index in range(len(self.joint_children)):
            for child_joint_index in self.joint_children[parent_joint_index]:
                self.edge_list.append([parent_joint_index, child_joint_index])

    def _build_execution_order(self):
        self.execution_order = []
        visited = set()
        def visit(jI):
            if jI in visited: return
            p = self.joint_parents[jI]
            if p != -1 and p not in visited: visit(p)
            self.execution_order.append(jI)
            visited.add(jI)
        for jI in range(self.joint_count): visit(jI)

    def setOrigSeqIndex(self, index):
        self.orig_seq_index = min(index, len(self.orig_sequences) - 1 )
        self.orig_seq_changed = True

    def setOrigSeqStartFrameIndex(self, index):
        self.orig_seq_start_frame_index = min(index, self.orig_sequences[self.orig_seq_index].shape[0] - self.seq_length )
        self.orig_seq_changed = True

    def setOrigSeqFrameCount(self, count):
        self.orig_seq_frame_count = min(count, self.seq_length)
        self.orig_seq_changed = True

    def setOrigSeqBlend(self, blend):
        self.orig_seq_blend_factor = max(min(1.0, blend), 0.0)
        self.orig_seq_changed = True

    def setRandRange(self, rand):
        self.seq_rand_range = rand

    def changeSequence(self):
        orig_seq = np.copy(self.orig_sequences[self.orig_seq_index][self.orig_seq_start_frame_index:self.orig_seq_start_frame_index + self.orig_seq_frame_count, ...])

        if self.train_root_trajectory:
            orig_seq[:, :3] = (orig_seq[:, :3] - self.root_pos_mean.flatten()) / self.root_pos_std.flatten()
            orig_seq = torch.from_numpy(orig_seq).to(self.device)

        if self.orig_seq_frame_count < self.seq_length:
            self.motion_seq = torch.cat((orig_seq, self.motion_seq[:self.seq_length - self.orig_seq_frame_count, ...]), dim=0)
        else:
            self.motion_seq = orig_seq
        self.orig_seq_changed = False

    def setGaussianTemp(self, gaussian_temp):
        self.gaussian_temp = gaussian_temp

    def setPiTemp(self, pi_temperature):
        self.pi_temperature = pi_temperature

    def setTopP(self, top_p):
        self.top_p = top_p

    def _orthogonalize_pose(self, pose):
        pose_shape = pose.shape
        if self.train_root_trajectory:
            root_pos = pose[..., :3]
            rot_6d = pose[..., 3:]
        else:
            rot_6d = pose

        rot_6d = rot_6d.view(*pose_shape[:-1], self.joint_count, 6)
        rot_mat = rot_to.r6d_to_mat(rot_6d)
        rot_6d_norm = rot_to.mat_to_r6d(rot_mat)
        rot_6d_norm = rot_6d_norm.view(*pose_shape[:-1], -1)

        if self.train_root_trajectory:
            return torch.cat((root_pos, rot_6d_norm), dim=-1)
        else:
            return rot_6d_norm

    def update(self):
        if self.orig_seq_changed:
            self.changeSequence()

        with torch.no_grad():
            log_pi, mu, sigma = self.model(self.motion_seq.unsqueeze(0), return_sequence=False)
            
            pred_pose_norm = self._sample_mdn(log_pi, mu, sigma, gaussian_temp=self.gaussian_temp, pi_temperature=self.pi_temperature, top_p=self.top_p)
            
            # Apply 1 Euro Filter and enforce orthogonality
            filtered_pose = self.one_euro_filter(pred_pose_norm)
            self.pred_pose = self._orthogonalize_pose(filtered_pose)

            self.motion_seq = torch.cat([self.motion_seq[1:,:], self.pred_pose.squeeze(0)], axis=0)

            # Reconstruct pos and quat
            pred_numpy = self.pred_pose.detach().cpu().numpy().squeeze(0)

            if self.train_root_trajectory:
                root_traj = pred_numpy[:, :3]
                root_traj = (root_traj * self.root_pos_std.flatten()) + self.root_pos_mean.flatten()
                rot_seq = pred_numpy[:, 3:]
            else:
                root_traj = np.zeros((1, 3), dtype=np.float32)
                rot_seq = pred_numpy

            rot_seq_6d = np.reshape(rot_seq, (1, 1, self.joint_count, 6))
            rot_sequence_tensor = torch.tensor(rot_seq_6d).to(self.device)
            rot_matrices = rot_to.r6d_to_mat(rot_sequence_tensor)

            root_trajectory_tensor = torch.tensor(root_traj).unsqueeze(0).to(self.device)
            self.synth_pose_wpos, self.synth_pose_wrot_mat = self._forward_kinematics(rot_matrices, root_trajectory_tensor)

            self.synth_pose_wpos = self.synth_pose_wpos.detach().cpu().numpy().squeeze()
            self.synth_pose_wpos = self.synth_pose_wpos.reshape((self.joint_count, 3))

            wrot_matrix_numpy = self.synth_pose_wrot_mat.detach().cpu().numpy().squeeze()
            self.synth_pose_wrot = rot_np.mat_to_quat(wrot_matrix_numpy)

            lrot_6d_numpy = rot_seq_6d.squeeze()
            self.synth_pose_lrot = rot_np.r6d_to_quat(lrot_6d_numpy)

    def _sample_mdn(self, log_pi, mu, sigma, gaussian_temp=0.1, pi_temperature=1.5, top_p=0.9):
        batch_size, seq_len, K = log_pi.shape

        scaled_log_pi = log_pi / pi_temperature
        pi_probs = nnF.softmax(scaled_log_pi, dim=-1)

        if top_p < 1.0:
            sorted_probs, sorted_indices = torch.sort(pi_probs, descending=True, dim=-1)
            cumulative_probs = torch.cumsum(sorted_probs, dim=-1)

            sorted_indices_to_remove = cumulative_probs > top_p
            sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
            sorted_indices_to_remove[..., 0] = 0

            indices_to_remove = torch.zeros_like(pi_probs, dtype=torch.bool)
            indices_to_remove.scatter_(dim=-1, index=sorted_indices, src=sorted_indices_to_remove)

            pi_probs[indices_to_remove] = 0.0
            pi_probs = pi_probs / pi_probs.sum(dim=-1, keepdim=True)

        pi_probs_flat = pi_probs.reshape(-1, K)
        chosen_idx = torch.multinomial(pi_probs_flat, 1)
        chosen_idx = chosen_idx.view(batch_size, seq_len, 1, 1)

        idx_expanded = chosen_idx.expand(batch_size, seq_len, 1, mu.size(-1))

        chosen_mu = torch.gather(mu, 2, idx_expanded).squeeze(2)
        chosen_sigma = torch.gather(sigma, 2, idx_expanded).squeeze(2)

        epsilon = torch.randn_like(chosen_mu) * gaussian_temp
        sampled_pose = chosen_mu + chosen_sigma * epsilon
        
        return self._orthogonalize_pose(sampled_pose)

    def _forward_kinematics(self, rotation_matrices, root_positions):
        t_offsets = torch.tensor(self.joint_offsets).to(self.device)
        expanded_offsets = t_offsets.expand(rotation_matrices.shape[0], rotation_matrices.shape[1], self.joint_offsets.shape[0], self.joint_offsets.shape[1]).unsqueeze(-1)
        
        positions_world = [None] * self.joint_count
        rotations_world = [None] * self.joint_count
        
        for jI in self.execution_order:
            if self.joint_parents[jI] == -1:
                positions_world[jI] = root_positions
                rotations_world[jI] = rotation_matrices[:, :, jI]
            else:
                parent_idx = self.joint_parents[jI]
                parent_rot = rotations_world[parent_idx]
                parent_pos = positions_world[parent_idx]
                local_offset = expanded_offsets[:, :, jI]
                
                rotated_offset = torch.matmul(parent_rot, local_offset).squeeze(-1)
                positions_world[jI] = rotated_offset + parent_pos

                if len(self.joint_children[jI]) > 0:
                    local_rot = rotation_matrices[:, :, jI]
                    rotations_world[jI] = torch.matmul(parent_rot, local_rot)
                else:
                    rotations_world[jI] = parent_rot
                    
        return torch.stack(positions_world, dim=3).permute(0, 1, 3, 2), torch.stack(rotations_world, dim=3).permute(0, 1, 3, 2, 4)