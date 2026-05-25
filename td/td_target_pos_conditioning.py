# -------------------------------------------------------------------------------------------------
# Motion Continuation Model - Training Script (Autonomous / Interactive Target Mode)
# Employs a Transformer Decoder MDN Architecture
# -------------------------------------------------------------------------------------------------

import torch
import torch.nn as nn
import torch.nn.functional as nnF
from torch.utils.data import Dataset, DataLoader

import numpy as np
import math
import time
import json
import os
import copy
import matplotlib.pyplot as plt
from scipy.signal import savgol_filter

from common import utils
from common import bvh_tools as bvh
from common import fbx_tools as fbx
from common import mocap_tools as mocap
from common.pose_renderer import PoseRenderer

from common.rotation_utils_numpy import RotationUtilsNumpy as rot_np
from common.rotation_utils_torch import RotationUtilsTorch as rot_to

# -------------------------------------------------------------------------------------------------
# Compute Unit
# -------------------------------------------------------------------------------------------------
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"Using {device} device")

# -------------------------------------------------------------------------------------------------
# Mocap Settings
# -------------------------------------------------------------------------------------------------
mocap_file_path = "E:/Data/mocap/stocos/Solos/Canal_14-08-2023/fbx_50hz/"
mocap_files = ["Muriel_Embodied_Machine_variation.fbx"]
mocap_valid_time_ranges = [ [ [ 4.0, 127.0 ] ] ]  # in seconds
mocap_pos_scale = 1.0
mocap_fps = 50

mocap_loss_weights_file = None
train_root_trajectory = True

# -------------------------------------------------------------------------------------------------
# Save Paths Settings
# -------------------------------------------------------------------------------------------------
save_path = "results_Stocos_EmbodiedMachine_TPConditioned/"
save_weights_path = save_path + "weights/"
save_history_path = save_path + "history/"
save_anims_path = save_path + "anims/"
save_anim_formats = ["gif", "fbx"]

os.makedirs(save_weights_path, exist_ok=True)
os.makedirs(save_history_path, exist_ok=True)
os.makedirs(save_anims_path, exist_ok=True)

# -------------------------------------------------------------------------------------------------
# Model Settings
# -------------------------------------------------------------------------------------------------
mdn_num_mixtures = 20
decoder_layer_count = 6 
decoder_head_count = 8
decoder_embed_dim = 512 
decoder_ff_dim = 2048 
decoder_dropout = 0.1

cond_dim = 3  # 3D spatial target conditioning (X, Y, Z)

# -------------------------------------------------------------------------------------------------
# Training Settings
# -------------------------------------------------------------------------------------------------
batch_size = 32
test_percentage = 0.1

seq_input_length = 64
seq_output_length = 2
seq_offset = 4

pi_temperature = 2.0
learning_rate = 1e-4
pos_loss_scale = 0.1
rot_loss_scale = 1.0
traj_loss_scale = 0.1
nll_loss_scale = 1.0
teacher_forcing_prob = 0.5
model_save_interval = 50

epochs = 200
save_history = True
save_weights = True
load_weights = False
decoder_weights_file = ""

# -------------------------------------------------------------------------------------------------
# Render Settings
# -------------------------------------------------------------------------------------------------
view_ele, view_azi = 90.0, -90.0
view_line_width, view_size = 1.0, 4.0

# -------------------------------------------------------------------------------------------------
# Utility: Variable Timestamp Resampling
# -------------------------------------------------------------------------------------------------
def resample_mocap_data(mocap_data, target_fps, time_ranges):
    # [Unchanged logic from original script]
    times_dict = mocap_data["motion"].get("times", {})
    joints = mocap_data["skeleton"]["joints"]
    num_joints = len(joints)
    pos_local = mocap_data["motion"]["pos_local"]
    rot_local_euler = mocap_data["motion"]["rot_local_euler"]
    
    def get_joint_data(data, j_idx): return data[j_idx] if isinstance(data, list) else data[:, j_idx, :]
            
    joint_times_list = []
    for j_idx, j_name in enumerate(joints):
        if j_name in times_dict: j_times = times_dict[j_name]
        else:
            orig_fps = mocap_data.get("frame_rate", target_fps)
            j_times = np.arange(len(get_joint_data(pos_local, j_idx))) / orig_fps
        joint_times_list.append(j_times)
        
    resampled_segments = []
    for t_range in time_ranges:
        target_times = np.arange(t_range[0], t_range[1], 1.0 / target_fps)
        num_frames = len(target_times)
        new_pos_local = np.zeros((num_frames, num_joints, 3))
        new_rot_local_euler = np.zeros((num_frames, num_joints, 3))
        
        for j_idx in range(num_joints):
            j_times = joint_times_list[j_idx]
            j_pos, j_rot = get_joint_data(pos_local, j_idx), get_joint_data(rot_local_euler, j_idx)
            
            if len(j_times) == 0: continue
            if len(j_times) == 1:
                new_pos_local[:, j_idx, :] = j_pos[0]
                new_rot_local_euler[:, j_idx, :] = j_rot[0]
                continue
                
            for i in range(3): new_pos_local[:, j_idx, i] = np.interp(target_times, j_times, j_pos[:, i])
                
            j_rot_rad = np.deg2rad(j_rot)
            j_rot_deg_unwrapped = np.rad2deg(np.unwrap(j_rot_rad, axis=0))
            for i in range(3): new_rot_local_euler[:, j_idx, i] = np.interp(target_times, j_times, j_rot_deg_unwrapped[:, i])
                
        segment_data = copy.deepcopy(mocap_data)
        segment_data["motion"]["pos_local"] = new_pos_local
        segment_data["motion"]["rot_local_euler"] = new_rot_local_euler
        segment_data["frame_rate"] = target_fps
        if "times" in segment_data["motion"]: del segment_data["motion"]["times"]
        resampled_segments.append(segment_data)
        
    return resampled_segments

# -------------------------------------------------------------------------------------------------
# Load Mocap Data & Create Dataset
# -------------------------------------------------------------------------------------------------
bvh_tools, fbx_tools, mocap_tools = bvh.BVH_Tools(), fbx.FBX_Tools(), mocap.Mocap_Tools()
all_mocap_data = []

for i, mocap_file in enumerate(mocap_files):
    print("process file ", mocap_file)
    valid_time_ranges = mocap_valid_time_ranges[i]
    
    if mocap_file.endswith(".bvh") or mocap_file.endswith(".BVH"):
        mocap_data_raw = mocap_tools.bvh_to_mocap(bvh_tools.load(os.path.join(mocap_file_path, mocap_file)))
        segments = resample_mocap_data(mocap_data_raw, mocap_fps, valid_time_ranges)
        for segment in segments:
            segment["motion"]["rot_local"] = mocap_tools.euler_to_quat_bvh(segment["motion"]["rot_local_euler"], segment["rot_sequence"])
            all_mocap_data.append(segment)
            
    elif mocap_file.endswith(".fbx") or mocap_file.endswith(".FBX"):
        mocap_data_raw = mocap_tools.fbx_to_mocap(fbx_tools.load(os.path.join(mocap_file_path, mocap_file)))[0] 
        segments = resample_mocap_data(mocap_data_raw, mocap_fps, valid_time_ranges)
        for segment in segments:
            segment["motion"]["rot_local"] = mocap_tools.euler_to_quat(segment["motion"]["rot_local_euler"], segment["rot_sequence"])
            all_mocap_data.append(segment)

for mocap_data in all_mocap_data:
    mocap_data["skeleton"]["offsets"] *= mocap_pos_scale
    mocap_data["motion"]["pos_local"] *= mocap_pos_scale
    if not train_root_trajectory:
        mocap_data["skeleton"]["offsets"][0, [0, 2]] = 0.0
        mocap_data["motion"]["pos_local"][:, 0, [0, 2]] = 0.0
    mocap_data["motion"]["rot_local"] = rot_np.quat_to_r6d(mocap_data["motion"]["rot_local"])

mocap_data = all_mocap_data[0]
joint_count = mocap_data["motion"]["rot_local"].shape[1]
joint_dim = 6
pose_dim = joint_count * joint_dim
input_dim = pose_dim + 3 if train_root_trajectory else pose_dim

offsets = mocap_data["skeleton"]["offsets"].astype(np.float32)
parents, children = mocap_data["skeleton"]["parents"], mocap_data["skeleton"]["children"]
edge_list = [[p, c] for p in range(len(children)) for c in children[p]]

joint_loss_weights = json.load(open(mocap_loss_weights_file))["joint_loss_weights"] if mocap_loss_weights_file else [1.0] * joint_count

X, y, all_excerpts = [], [], []

for mocap_data in all_mocap_data:
    pose_sequence = np.reshape(mocap_data["motion"]["rot_local"], (-1, pose_dim))
    if train_root_trajectory:
        pose_sequence = np.concatenate((mocap_data["motion"]["pos_local"][:, 0, :], pose_sequence), axis=1)

    for pI in np.arange(0, pose_sequence.shape[0] - seq_input_length - seq_output_length - 1, seq_offset):
        X_sample = pose_sequence[pI:pI+seq_input_length]
        Y_sample = pose_sequence[pI+seq_input_length:pI+seq_input_length+seq_output_length]
        X.append(X_sample); y.append(Y_sample)
        all_excerpts.extend([X_sample, Y_sample])

X, y, all_excerpts = np.array(X, dtype=np.float32), np.array(y, dtype=np.float32), np.concatenate(all_excerpts, axis=0)

if train_root_trajectory:
    root_pos_mean = np.mean(all_excerpts[:, :3], axis=0, keepdims=True)
    root_pos_std = np.std(all_excerpts[:, :3], axis=0, keepdims=True)
    root_pos_std[root_pos_std == 0] = 1.0
    X[:, :, :3] = (X[:, :, :3] - root_pos_mean) / root_pos_std
    y[:, :, :3] = (y[:, :, :3] - root_pos_mean) / root_pos_std
    root_pos_mean_tensor = torch.from_numpy(root_pos_mean).unsqueeze(0).to(device)
    root_pos_std_tensor = torch.from_numpy(root_pos_std).unsqueeze(0).to(device)

X, y = torch.from_numpy(X), torch.from_numpy(y)

class SequenceDataset(Dataset):
    def __init__(self, X, y): self.X, self.y = X, y
    def __len__(self): return self.X.shape[0]
    def __getitem__(self, idx): return self.X[idx, ...], self.y[idx, ...]

full_dataset = SequenceDataset(X, y)
test_size = int(test_percentage * len(full_dataset))
train_dataset, test_dataset = torch.utils.data.random_split(full_dataset, [len(full_dataset) - test_size, test_size])
train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False)

# -------------------------------------------------------------------------------------------------
# Create Models
# -------------------------------------------------------------------------------------------------
class PositionalEncoding(nn.Module):
    def __init__(self, dim_model, dropout_p, max_len):
        super().__init__()
        self.dropout = nn.Dropout(dropout_p)
        pos_encoding = torch.zeros(max_len, dim_model)
        positions_list = torch.arange(0, max_len, dtype=torch.float).view(-1, 1)
        division_term = torch.exp(torch.arange(0, dim_model, 2).float() * (-math.log(10000.0)) / dim_model)
        pos_encoding[:, 0::2] = torch.sin(positions_list * division_term)
        pos_encoding[:, 1::2] = torch.cos(positions_list * division_term)
        self.register_buffer("pos_encoding", pos_encoding.unsqueeze(0))

    def forward(self, token_embedding):
        return self.dropout(token_embedding + self.pos_encoding[:, :token_embedding.size(1), :])

class MDNLayer(nn.Module):
    def __init__(self, hidden_dim, out_dim, num_mixtures=5):
        super().__init__()
        self.num_mixtures, self.out_dim = num_mixtures, out_dim
        self.pi_head = nn.Linear(hidden_dim, num_mixtures)
        self.mu_head = nn.Linear(hidden_dim, num_mixtures * out_dim)
        self.sigma_head = nn.Linear(hidden_dim, num_mixtures * out_dim)

    def forward(self, x):
        batch_size, seq_len, _ = x.shape
        log_pi = nnF.log_softmax(self.pi_head(x), dim=-1) 
        mu = self.mu_head(x).view(batch_size, seq_len, self.num_mixtures, self.out_dim) 
        sigma = nnF.elu(self.sigma_head(x).view(batch_size, seq_len, self.num_mixtures, self.out_dim)) + 1.0 + 1e-6 
        return log_pi, mu, sigma

class TransformerDecoderMDN(nn.Module):
    def __init__(self, motion_dim, embed_dim, num_heads, num_decoder_layers, ff_dim, dropout_p, pos_encoding_max_length, num_mixtures=5):
        super().__init__()
        self.embed_dim = embed_dim
        self.motion2embed = nn.Linear(motion_dim, embed_dim)
        self.positional_encoder = PositionalEncoding(embed_dim, dropout_p, pos_encoding_max_length * 2)
        self.encoder_layer = nn.TransformerEncoderLayer(embed_dim, num_heads, ff_dim, dropout_p, activation='gelu', batch_first=True)
        self.transformer = nn.TransformerEncoder(self.encoder_layer, num_layers=num_decoder_layers)
        
        # MDN predicts both Pose (input_dim) and Target (cond_dim)
        self.mdn = MDNLayer(embed_dim, input_dim + cond_dim, num_mixtures)

    def forward(self, motion_data, return_sequence=False):
        tgt_mask = nn.Transformer.generate_square_subsequent_mask(motion_data.size(1), device=motion_data.device)
        motion_embedded = self.positional_encoder(self.motion2embed(motion_data) * math.sqrt(self.embed_dim))
        decoder_out = self.transformer(motion_embedded, mask=tgt_mask, is_causal=True)
        log_pi, mu, sigma = self.mdn(decoder_out)
        return (log_pi, mu, sigma) if return_sequence else (log_pi[:, -1:, :], mu[:, -1:, :, :], sigma[:, -1:, :, :])

decoder = TransformerDecoderMDN(motion_dim=input_dim + cond_dim, embed_dim=decoder_embed_dim, num_heads=decoder_head_count, num_decoder_layers=decoder_layer_count, ff_dim=decoder_ff_dim, dropout_p=decoder_dropout, pos_encoding_max_length=seq_input_length, num_mixtures=mdn_num_mixtures).to(device)

if load_weights: decoder.load_state_dict(torch.load(decoder_weights_file))

# -------------------------------------------------------------------------------------------------
# Training Losses & Kinetics
# -------------------------------------------------------------------------------------------------
optimizer = torch.optim.Adam(decoder.parameters(), lr=learning_rate)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=50, gamma=0.336)
joint_loss_weights_t = torch.tensor(joint_loss_weights, dtype=torch.float32).reshape(1, 1, -1).to(device)

def forward_kinematics(rotation_matrices, root_positions):
    t_offsets = torch.tensor(offsets).to(device)
    expanded_offsets = t_offsets.expand(rotation_matrices.shape[0], rotation_matrices.shape[1], offsets.shape[0], offsets.shape[1]).unsqueeze(-1)
    positions_world, rotations_world = [], []
    for jI in range(offsets.shape[0]):
        if parents[jI] == -1:
            positions_world.append(root_positions)
            rotations_world.append(rotation_matrices[:, :, 0])
        else:
            parent_rot = rotations_world[parents[jI]]
            rotated_offset = torch.matmul(parent_rot, expanded_offsets[:, :, jI]).squeeze(-1)
            positions_world.append(rotated_offset + positions_world[parents[jI]])
            rotations_world.append(torch.matmul(parent_rot, rotation_matrices[:, :, jI]) if len(children[jI]) > 0 else parent_rot)
    return torch.stack(positions_world, dim=3).permute(0, 1, 3, 2)

def pos_loss(y, yhat):
    if train_root_trajectory:
        y_root_traj = (y[:, :, :3] * root_pos_std_tensor) + root_pos_mean_tensor
        yhat_root_traj = (yhat[:, :, :3] * root_pos_std_tensor) + root_pos_mean_tensor
        y_rot_6d = y[:, :, 3:].reshape(y.shape[0], y.shape[1], joint_count, 6)
        yhat_rot_6d = yhat[:, :, 3:].reshape(yhat.shape[0], yhat.shape[1], joint_count, 6)
    else:
        y_root_traj, yhat_root_traj = torch.zeros((y.shape[0], y.shape[1], 3)).to(device), torch.zeros((yhat.shape[0], yhat.shape[1], 3)).to(device)
        y_rot_6d = y.reshape(y.shape[0], y.shape[1], joint_count, 6)
        yhat_rot_6d = yhat.reshape(yhat.shape[0], yhat.shape[1], joint_count, 6)

    y_pos = forward_kinematics(rot_to.r6d_to_mat(y_rot_6d), y_root_traj)
    yhat_pos = forward_kinematics(rot_to.r6d_to_mat(yhat_rot_6d), yhat_root_traj)
    return torch.mean(torch.norm(y_pos - yhat_pos, dim=3) * joint_loss_weights_t)

def rot_loss(y, yhat):
    if train_root_trajectory: y, yhat = y[:, :, 3:], yhat[:, :, 3:]
    y_mat = rot_to.r6d_to_mat(y.reshape(y.shape[0], y.shape[1], joint_count, 6))
    yhat_mat = rot_to.r6d_to_mat(yhat.reshape(yhat.shape[0], yhat.shape[1], joint_count, 6))
    trace = torch.diagonal(torch.matmul(y_mat.transpose(-1, -2), yhat_mat), dim1=-2, dim2=-1).sum(-1)
    return torch.mean(torch.acos(torch.clamp((trace - 1) / 2, -0.9999, 0.9999)) * joint_loss_weights_t)

def mdn_nll_loss(log_pi, mu, sigma, target):
    target = target.unsqueeze(2)
    var = sigma ** 2
    log_normal = torch.sum(-0.5 * math.log(2 * math.pi) - torch.log(sigma) - 0.5 * ((target - mu) ** 2 / var), dim=-1) 
    return -torch.logsumexp(log_pi + log_normal, dim=-1).mean()

def sample_mdn(log_pi, mu, sigma, gaussian_temp=0.2, pi_temperature=1.5, top_p=0.9):
    batch_size, seq_len, K = log_pi.shape
    pi_probs = nnF.softmax(log_pi / pi_temperature, dim=-1)

    if top_p < 1.0:
        sorted_probs, sorted_indices = torch.sort(pi_probs, descending=True, dim=-1)
        cumulative_probs = torch.cumsum(sorted_probs, dim=-1)
        sorted_indices_to_remove = cumulative_probs > top_p
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0
        indices_to_remove = torch.zeros_like(pi_probs, dtype=torch.bool).scatter_(dim=-1, index=sorted_indices, src=sorted_indices_to_remove)
        pi_probs[indices_to_remove] = 0.0
        pi_probs = pi_probs / pi_probs.sum(dim=-1, keepdim=True)

    chosen_idx = torch.multinomial(pi_probs.reshape(-1, K), 1).view(batch_size, seq_len, 1, 1)
    idx_expanded = chosen_idx.expand(batch_size, seq_len, 1, mu.size(-1))
    chosen_mu = torch.gather(mu, 2, idx_expanded).squeeze(2) 
    chosen_sigma = torch.gather(sigma, 2, idx_expanded).squeeze(2) 

    sampled_val = chosen_mu + chosen_sigma * (torch.randn_like(chosen_mu) * gaussian_temp)
    
    pose_pred = sampled_val[:, :, :input_dim]
    target_pred = sampled_val[:, :, input_dim:]

    if train_root_trajectory:
        root_pred, rot_pred = pose_pred[:, :, :3], pose_pred[:, :, 3:]
    else:
        rot_pred = pose_pred

    rot_pred_6d_norm = rot_to.mat_to_r6d(rot_to.r6d_to_mat(rot_pred.reshape(batch_size, seq_len, joint_count, 6))).reshape(batch_size, seq_len, -1)

    return torch.cat((root_pred, rot_pred_6d_norm, target_pred), dim=-1) if train_root_trajectory else torch.cat((rot_pred_6d_norm, target_pred), dim=-1)

def loss(log_pi, mu, sigma, target_poses_cond):
    _nll_loss = mdn_nll_loss(log_pi, mu, sigma, target_poses_cond)
    pred_poses_cond = sample_mdn(log_pi, mu, sigma, pi_temperature=pi_temperature)

    target_poses, pred_poses = target_poses_cond[:, :, :input_dim], pred_poses_cond[:, :, :input_dim]
    _pos_loss = pos_loss(target_poses, pred_poses)
    _rot_loss = rot_loss(target_poses, pred_poses)

    _total_loss = (_nll_loss * nll_loss_scale) + (_pos_loss * pos_loss_scale) + (_rot_loss * rot_loss_scale)

    if train_root_trajectory:
        _total_loss += (torch.mean((target_poses[:, :, :3] - pred_poses[:, :, :3]) ** 2) * traj_loss_scale)

    # Penalize predicted target error
    _total_loss += (torch.mean((target_poses_cond[:, :, input_dim:] - pred_poses_cond[:, :, input_dim:]) ** 2) * traj_loss_scale)

    return _total_loss, _nll_loss, _pos_loss, _rot_loss

def train_step(pose_sequences, target_poses, teacher_forcing):
    decoder.train()
    output_poses_length = target_poses.shape[1]
    
    # Ground truth target location
    target_locations = target_poses[:, -1:, :3]
    target_cond_for_loss = target_locations.expand(-1, target_poses.size(1), -1)
    target_poses_cond = torch.cat([target_poses, target_cond_for_loss], dim=-1)

    if teacher_forcing:
        _full_input = torch.cat((pose_sequences, target_poses[:, :-1, :]), dim=1)
        _full_input_cond = torch.cat([_full_input, target_locations.expand(-1, _full_input.size(1), -1)], dim=-1)
        log_pi, mu, sigma = decoder(_full_input_cond, return_sequence=True)

        _log_pi_for_loss = log_pi[:, -output_poses_length:, :]
        _mu_for_loss = mu[:, -output_poses_length:, :, :]
        _sigma_for_loss = sigma[:, -output_poses_length:, :, :]
        _target_poses_for_loss = target_poses_cond
    else:
        _input_poses = pose_sequences 
        current_target_cond = target_locations.expand(-1, _input_poses.size(1), -1)
        _log_pi_list, _mu_list, _sigma_list = [], [], []

        for _ in range(output_poses_length):
            log_pi, mu, sigma = decoder(torch.cat([_input_poses, current_target_cond], dim=-1), return_sequence=False)
            _log_pi_list.append(log_pi)
            _mu_list.append(mu)
            _sigma_list.append(sigma)

            _pred_pose_cond = sample_mdn(log_pi, mu, sigma, pi_temperature=pi_temperature)
            _pred_pose = _pred_pose_cond[:, :, :input_dim]
            
            # Autoregressive Update: Next step uses model's predicted target
            current_target_cond = _pred_pose_cond[:, :, input_dim:].expand(-1, _input_poses.size(1), -1).detach().clone()
            
            _input_poses = torch.cat((_input_poses[:, 1:, :].detach().clone(), _pred_pose.detach().clone()), axis=1)

        _log_pi_for_loss = torch.cat(_log_pi_list, dim=1)
        _mu_for_loss = torch.cat(_mu_list, dim=1)
        _sigma_for_loss = torch.cat(_sigma_list, dim=1)
        _target_poses_for_loss = target_poses_cond

    _loss, _nll_loss, _pos_loss, _rot_loss = loss(_log_pi_for_loss, _mu_for_loss, _sigma_for_loss, _target_poses_for_loss) 
    optimizer.zero_grad(); _loss.backward(); optimizer.step()
    return _loss, _nll_loss, _pos_loss, _rot_loss

@torch.no_grad()
def test_step(pose_sequences, target_poses, teacher_forcing):
    decoder.eval() 
    output_poses_length = target_poses.shape[1]
    
    target_locations = target_poses[:, -1:, :3]
    target_cond_for_loss = target_locations.expand(-1, target_poses.size(1), -1)
    target_poses_cond = torch.cat([target_poses, target_cond_for_loss], dim=-1)

    if teacher_forcing:
        _full_input = torch.cat((pose_sequences, target_poses[:, :-1, :]), dim=1)
        _full_input_cond = torch.cat([_full_input, target_locations.expand(-1, _full_input.size(1), -1)], dim=-1)
        log_pi, mu, sigma = decoder(_full_input_cond, return_sequence=True)

        _log_pi_for_loss = log_pi[:, -output_poses_length:, :]
        _mu_for_loss = mu[:, -output_poses_length:, :, :]
        _sigma_for_loss = sigma[:, -output_poses_length:, :, :]
        _target_poses_for_loss = target_poses_cond
    else:
        _input_poses = pose_sequences 
        current_target_cond = target_locations.expand(-1, _input_poses.size(1), -1)
        _log_pi_list, _mu_list, _sigma_list = [], [], []

        for _ in range(output_poses_length):
            log_pi, mu, sigma = decoder(torch.cat([_input_poses, current_target_cond], dim=-1), return_sequence=False)
            _log_pi_list.append(log_pi); _mu_list.append(mu); _sigma_list.append(sigma)

            _pred_pose_cond = sample_mdn(log_pi, mu, sigma, pi_temperature=pi_temperature)
            current_target_cond = _pred_pose_cond[:, :, input_dim:].expand(-1, _input_poses.size(1), -1).detach().clone()
            _input_poses = torch.cat((_input_poses[:, 1:, :], _pred_pose_cond[:, :, :input_dim]), axis=1)

        _log_pi_for_loss = torch.cat(_log_pi_list, dim=1)
        _mu_for_loss = torch.cat(_mu_list, dim=1)
        _sigma_for_loss = torch.cat(_sigma_list, dim=1)
        _target_poses_for_loss = target_poses_cond

    return loss(_log_pi_for_loss, _mu_for_loss, _sigma_for_loss, _target_poses_for_loss) 

def train(train_dataloader, test_dataloader, epochs):
    loss_history = {"train": [], "test": [], "nll": [], "pos": [], "rot": []}
    for epoch in range(epochs):
        start = time.time()
        t_loss, n_loss, p_loss, r_loss = [], [], [], []

        for train_batch in train_dataloader:
            _loss, _nll_loss, _pos_loss, _rot_loss = train_step(train_batch[0].to(device), train_batch[1].to(device), np.random.uniform() < teacher_forcing_prob)
            t_loss.append(_loss.item()); n_loss.append(_nll_loss.item()); p_loss.append(_pos_loss.item()); r_loss.append(_rot_loss.item())

        te_loss = []
        for test_batch in test_dataloader:
            _loss, _, _, _ = test_step(test_batch[0].to(device), test_batch[1].to(device), np.random.uniform() < teacher_forcing_prob)
            te_loss.append(_loss.item())

        if epoch % model_save_interval == 0 and save_weights:
            torch.save(decoder.state_dict(), f"{save_weights_path}decoder_weights_epoch_{epoch}.pt")

        loss_history["train"].append(np.mean(t_loss))
        loss_history["test"].append(np.mean(te_loss))
        loss_history["nll"].append(np.mean(n_loss))
        loss_history["pos"].append(np.mean(p_loss))
        loss_history["rot"].append(np.mean(r_loss))
        scheduler.step()

        print(f"epoch {epoch + 1} : train: {np.mean(t_loss):01.4f} test: {np.mean(te_loss):01.4f} nll {np.mean(n_loss):01.4f} pos {np.mean(p_loss):01.4f} rot {np.mean(r_loss):01.4f} time {time.time()-start:01.2f}")

    return loss_history

# -------------------------------------------------------------------------------------------------
# Inference and Rendering
# -------------------------------------------------------------------------------------------------

def export_sequence_anim(pose_sequence, file_name):
    pose_count = pose_sequence.shape[0]
    if train_root_trajectory:
        root_trajectory = pose_sequence[:, :3]
        rot_sequence = pose_sequence[:, 3:]
    else:
        root_trajectory = np.zeros((pose_count, 3), dtype=np.float32)
        rot_sequence = pose_sequence

    rot_sequence = np.reshape(rot_sequence, (pose_count, joint_count, 6))
    rot_sequence_tensor = torch.tensor(np.expand_dims(rot_sequence, axis=0)).to(device)
    rot_matrices = rot_to.r6d_to_mat(rot_sequence_tensor)

    root_trajectory = torch.tensor(np.expand_dims(root_trajectory, axis=0)).to(device)
    skel_sequence = forward_kinematics(rot_matrices, root_trajectory)
    skel_sequence = skel_sequence.detach().cpu().numpy().squeeze()

    view_min, view_max = utils.get_equal_mix_max_positions(skel_sequence)
    skel_images = poseRenderer.create_pose_images(skel_sequence, view_min, view_max, view_ele, view_azi, view_line_width, view_size, view_size)
    skel_images[0].save(file_name, save_all=True, append_images=skel_images[1:], optimize=False, duration=33.0, loop=0)

def export_sequence_bvh(pose_sequence, file_name):
    pose_count = pose_sequence.shape[0]
    
    if train_root_trajectory:
        root_trajectory = pose_sequence[:, :3]
        rot_sequence = pose_sequence[:, 3:]
    else:
        root_trajectory = np.zeros((pose_count, 3), dtype=np.float32)
        rot_sequence = pose_sequence

    pred_dataset = {
        "frame_rate": mocap_data["frame_rate"],
        "rot_sequence": mocap_data["rot_sequence"],
        "skeleton": mocap_data["skeleton"],
        "motion": {}
    }

    # set joint local positions
    # the root joint gets its local position from the trajectory, all other joints from the offsets
    pos_local = np.repeat(np.expand_dims(pred_dataset["skeleton"]["offsets"], axis=0), pose_count, axis=0)
    pos_local[:, 0, :] = root_trajectory
    pred_dataset["motion"]["pos_local"] = pos_local

    # Convert 6D network output to Quaternions, then to Euler Angles 
    rot_seq_6d = np.reshape(rot_sequence, (pose_count, joint_count, 6))
    pred_dataset["motion"]["rot_local"] = rot_np.r6d_to_quat(rot_seq_6d)
    
    # Use the euler conversion work-around specifically designed for BVHs in this mocap_tools version
    pred_dataset["motion"]["rot_local_euler"] = mocap_tools.quat_to_euler_bvh(
        pred_dataset["motion"]["rot_local"], 
        pred_dataset["rot_sequence"]
    )

    # Use the internal mocap_to_bvh compiler
    pred_bvh = mocap_tools.mocap_to_bvh(pred_dataset)
    
    # Write the BVH to disk using the standard bvh_tools script structure
    bvh_tools.write(pred_bvh, file_name) 

def export_sequence_fbx(pose_sequence, file_name):
    pose_count = pose_sequence.shape[0]
    if train_root_trajectory:
        root_trajectory = pose_sequence[:, :3]
        rot_sequence = pose_sequence[:, 3:]
    else:
        root_trajectory = np.zeros((pose_count, 3), dtype=np.float32)
        rot_sequence = pose_sequence

    pred_dataset = {
        "frame_rate": mocap_data["frame_rate"],
        "rot_sequence": mocap_data["rot_sequence"],
        "skeleton": mocap_data["skeleton"],
        "motion": {}
    }

    pos_local = np.repeat(np.expand_dims(pred_dataset["skeleton"]["offsets"], axis=0), pose_count, axis=0)
    if train_root_trajectory: pos_local[:, 0, :] = root_trajectory
    pred_dataset["motion"]["pos_local"] = pos_local

    rot_seq_6d = np.reshape(rot_sequence, (pose_count, joint_count, 6))
    pred_dataset["motion"]["rot_local"] = rot_np.r6d_to_quat(rot_seq_6d)
    pred_dataset["motion"]["rot_local_euler"] = mocap_tools.quat_to_euler(pred_dataset["motion"]["rot_local"], pred_dataset["rot_sequence"])
    pred_fbx = mocap_tools.mocap_to_fbx([pred_dataset])
    fbx_tools.write(pred_fbx, file_name)

def smooth_motion(pred_poses, window_length=9, poly_order=3):
    pose_count = pred_poses.shape[0]
    if pose_count > window_length:
        pred_poses_tensor = torch.from_numpy(savgol_filter(pred_poses, window_length, poly_order, axis=0))
        root_smoothed, rot_smoothed = (pred_poses_tensor[:, :3], pred_poses_tensor[:, 3:]) if train_root_trajectory else (None, pred_poses_tensor)
        rot_pred_6d_norm = rot_to.mat_to_r6d(rot_to.r6d_to_mat(rot_smoothed.reshape(pose_count, joint_count, 6))).reshape(pose_count, -1)
        return torch.cat((root_smoothed, rot_pred_6d_norm), dim=-1).numpy() if train_root_trajectory else rot_pred_6d_norm.numpy()
    return pred_poses

@torch.no_grad()
def create_pred_sequence(pose_sequence, pose_count, initial_target_location, autonomous=True, interactive_target_stream=None):
    start_seq = pose_sequence
    if train_root_trajectory:
        start_seq[:, :3] = (start_seq[:, :3] - root_pos_mean.flatten()) / root_pos_std.flatten()

    start_seq = torch.reshape(torch.from_numpy(start_seq).to(device), (seq_input_length, input_dim))
    
    target_norm = (initial_target_location - root_pos_mean.flatten()) / root_pos_std.flatten()
    current_target_cond = torch.from_numpy(target_norm).to(device).float().view(1, 1, 3).expand(1, seq_input_length, 3)

    next_seq = start_seq
    pred_poses = []

    for i in range(pose_count):
        if not autonomous and interactive_target_stream is not None:
            user_tgt_norm = (interactive_target_stream[i] - root_pos_mean.flatten()) / root_pos_std.flatten()
            current_target_cond = torch.from_numpy(user_tgt_norm).to(device).float().view(1, 1, 3).expand(1, seq_input_length, 3)
            
        log_pi, mu, sigma = decoder(torch.cat([next_seq.unsqueeze(0), current_target_cond], dim=-1), return_sequence=False)
        pred_pose_cond_norm = sample_mdn(log_pi, mu, sigma, pi_temperature=pi_temperature)
        
        pred_pose_norm = pred_pose_cond_norm[:, :, :input_dim]
        pred_target_norm = pred_pose_cond_norm[:, :, input_dim:]
        
        if autonomous:
            current_target_cond = pred_target_norm.expand(1, seq_input_length, 3)
            
        pred_pose_norm = pred_pose_norm.reshape(1, input_dim)
        pred_poses.append(pred_pose_norm)
        next_seq = torch.cat([next_seq[1:,:], pred_pose_norm], axis=0)

    pred_poses = torch.cat(pred_poses, dim=0).detach().cpu().numpy()
    if train_root_trajectory:
        pred_poses[:, :3] = (pred_poses[:, :3] * root_pos_std.flatten()) + root_pos_mean.flatten()

    return smooth_motion(pred_poses)

# -------------------------------------------------------------------------------------------------
# Run Training
# -------------------------------------------------------------------------------------------------
if save_weights:
    loss_history = train(train_loader, test_loader, epochs)
    utils.save_loss_as_csv(loss_history, f"{save_history_path}history_{epochs}.csv")
    torch.save(decoder.state_dict(), f"{save_weights_path}decoder_weights_epoch_{epochs}.pt")

# -------------------------------------------------------------------------------------------------
# Run Inference and Export
# -------------------------------------------------------------------------------------------------

decoder.eval()
poseRenderer = PoseRenderer(edge_list)

# create original sequence
orig_rot = all_mocap_data[0]["motion"]["rot_local"].astype(np.float32)
orig_rot = np.reshape(orig_rot, (-1, pose_dim))

if train_root_trajectory:
    orig_pos = all_mocap_data[0]["motion"]["pos_local"][:, 0, :].astype(np.float32)
    orig_sequence = np.concatenate((orig_pos, orig_rot), axis=1)
else:
    orig_sequence = orig_rot

seq_index = 0
# Defensively bound start/lengths just in case user-supplied valid time cuts sequence shorter than arbitrary 1000/10000 constants
seq_start = min(1000, max(0, len(orig_sequence) - seq_input_length - 2)) 
seq_length = min(10000, len(orig_sequence) - seq_start)

if "gif" in save_anim_formats:
    export_sequence_anim(orig_sequence[seq_start:seq_start+seq_length], "{}orig_sequence_seq_start_{}_length_{}.gif".format(save_anims_path, seq_start, seq_length))
if "fbx" in save_anim_formats:
    export_sequence_fbx(orig_sequence[seq_start:seq_start+seq_length], "{}orig_sequence_seq_start_{}_length_{}.fbx".format(save_anims_path, seq_start, seq_length))
if "bvh" in save_anim_formats:
    export_sequence_bvh(orig_sequence[seq_start:seq_start+seq_length], "{}orig_sequence_seq_start_{}_length_{}.bvh".format(save_anims_path, seq_start, seq_length))

num_divergent_runs = 4
noise_scale_rot = 0.02 # Tiny perturbation for rotations (quaternions)
noise_scale_pos = 0.5  # Perturbation for root trajectory (in dataset units, e.g., cm)

for run_id in range(num_divergent_runs):
    
    start_seq_raw = orig_sequence[seq_start:seq_start+seq_input_length].copy()
    
    # Generate random noise matching the shape of the start sequence
    noise = np.random.normal(loc=0.0, scale=1.0, size=start_seq_raw.shape).astype(np.float32)
    
    if train_root_trajectory:
        # Scale noise differently for trajectory vs rotations
        noise[:, :3] *= noise_scale_pos
        noise[:, 3:] *= noise_scale_rot
        
        perturbed_seq_raw = start_seq_raw + noise
        
        # ---------------------------------------------------------
        # Re-orthogonalize the perturbed 6D rotations (Gram-Schmidt)
        # ---------------------------------------------------------
        rot_6d = perturbed_seq_raw[:, 3:].reshape(seq_input_length, joint_count, 6)
        rot_6d_tensor = torch.from_numpy(rot_6d)
        
        x_raw = rot_6d_tensor[..., 0:3]
        y_raw = rot_6d_tensor[..., 3:6]
        
        x = nnF.normalize(x_raw, dim=-1)
        z = torch.cross(x, y_raw, dim=-1)
        z = nnF.normalize(z, dim=-1)
        y = torch.cross(z, x, dim=-1)
        
        rot_6d_norm = torch.cat((x, y), dim=-1).reshape(seq_input_length, -1).numpy()
        
        # Final model input
        start_seq_6d = np.concatenate((perturbed_seq_raw[:, :3], rot_6d_norm), axis=1)
        
    else:
        # Rotation only
        noise *= noise_scale_rot
        perturbed_seq_raw = start_seq_raw + noise
        
        # Re-orthogonalize
        rot_6d = perturbed_seq_raw.reshape(seq_input_length, joint_count, 6)
        rot_6d_tensor = torch.from_numpy(rot_6d)
        
        x_raw = rot_6d_tensor[..., 0:3]
        y_raw = rot_6d_tensor[..., 3:6]
        
        x = nnF.normalize(x_raw, dim=-1)
        z = torch.cross(x, y_raw, dim=-1)
        z = nnF.normalize(z, dim=-1)
        y = torch.cross(z, x, dim=-1)
        
        # Final model input
        start_seq_6d = torch.cat((x, y), dim=-1).reshape(seq_input_length, -1).numpy()
    
    print(f"Generating divergent run {run_id+1}/{num_divergent_runs}...")
    
    # create predicted sequence
    rng = np.random.default_rng()
    target_location_mins = np.array([-100.0, 50.0, -100.0], dtype=float)
    target_location_maxs = np.array([100.0, 150.0, 100.0], dtype=float)
    target_location_means = (target_location_mins + target_location_maxs) / 2.0
    target_location_stds  = (target_location_maxs - target_location_mins) / 6.0   # 6σ span roughly covers [min, max]
    target_location = rng.normal(loc=target_location_means, scale=target_location_stds)  # shape (3,)
    pred_sequence = create_pred_sequence(start_seq_6d, seq_length, target_location)
    
    if "gif" in save_anim_formats:
        export_sequence_anim(pred_sequence, "{}pred_sequence_epoch_{}_seq_start_{}_length_{}_run_{}.gif".format(save_anims_path, epochs, seq_start, seq_length, run_id))
    if "fbx" in save_anim_formats:
        export_sequence_fbx(pred_sequence, "{}pred_sequence_epoch_{}_seq_start_{}_length_{}_run_{}.fbx".format(save_anims_path, epochs, seq_start, seq_length, run_id))
    if "bvh" in save_anim_formats:
        export_sequence_bvh(pred_sequence, "{}pred_sequence_epoch_{}_seq_start_{}_length_{}_run_{}.bvh".format(save_anims_path, epochs, seq_start, seq_length, run_id))