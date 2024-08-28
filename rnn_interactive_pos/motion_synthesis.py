import torch
from torch import nn
import numpy as np

from common.quaternion import qmul, qrot, qnormalize_np, qfix
from common.quaternion_torch import slerp

config = {"skeleton": None,
          "model": None,
          "seq_length": 64,
          "orig_sequences": [],
          "orig_seq_index": 0,
          "device": "cuda"
          }

"""
test_rot = torch.ones([4], dtype=torch.float32)
test_rot.shape

test_rot

test_rot_norm = nn.functional.normalize(test_rot, p=2, dim=0)

test_rot_norm
"""

class MotionSynthesis():
    
    def __init__(self, config):
        self.skeleton = config["skeleton"]
        self.model = config["model"]
        self.seq_length = config["seq_length"]
        self.device = config["device"]
        self.orig_sequences = config["orig_sequences"]
        self.orig_seq_index = config["orig_seq_index"]
        self.orig_seq_start_frame_index = 0
        self.orig_seq_frame_count = self.seq_length
        self.orig_seq_blend_factor = 1.0

        self.motion_seq = torch.from_numpy(self.orig_sequences[self.orig_seq_index][self.orig_seq_start_frame_index:self.orig_seq_start_frame_index + self.orig_seq_frame_count, ...]).to(self.device)
        
        self.orig_seq_changed = False
        
        self.joint_count = self.motion_seq.shape[1]
        self.joint_dim = self.motion_seq.shape[2]
        self.pose_dim = self.joint_count * self.joint_dim

        self.joint_children = self.skeleton ["children"]
        
        self._calc_pos_normalisation()
        
        self._create_edge_list()
        
        self.synth_pose_wpos = None
        
    def _create_edge_list(self):
        
        self.edge_list = []
        
        for parent_joint_index in range(len(self.joint_children)):
            for child_joint_index in self.joint_children[parent_joint_index]:
                self.edge_list.append([parent_joint_index, child_joint_index])
                
                
    def _calc_pos_normalisation(self):
         
         # calculate pose normalisation values
         # TODO: this value should have been stored during training and just retrieved here
         
         orig_sequence_all = np.concatenate(self.orig_sequences, axis=0)
 
         pose_mean = np.mean(orig_sequence_all, axis=0).flatten()
         pose_std = np.std(orig_sequence_all, axis=0).flatten()
         
         self.pose_mean = torch.tensor(pose_mean).reshape(1, 1, -1).to(self.device)
         self.pose_std = torch.tensor(pose_std).reshape(1, 1, -1).to(self.device)

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

    def changeSequence(self):
        
        orig_seq = torch.from_numpy(self.orig_sequences[self.orig_seq_index][self.orig_seq_start_frame_index:self.orig_seq_start_frame_index + self.orig_seq_frame_count, ... ]).to(self.device)
        
        if self.orig_seq_blend_factor >= 1.0:
         
            if self.orig_seq_frame_count < self.seq_length:
                #self.motion_seq =  torch.concat( (self.motion_seq[:self.seq_length - self.orig_seq_frame_count, ...], orig_seq), dim=0)
                self.motion_seq =  torch.concat( (orig_seq, self.motion_seq[:self.seq_length - self.orig_seq_frame_count, ...]), dim=0)
                
            else:
                self.motion_seq = orig_seq
        else:

            if self.orig_seq_frame_count < self.seq_length:
                
                orig_seq = orig_seq.reshape(-1, self.joint_dim)
                cur_seq  = self.motion_seq[:self.orig_seq_frame_count, ...].reshape(-1, self.joint_dim)
                blend_factor = torch.ones([orig_seq.shape[0]], dtype=torch.float32).to(self.device) * self.orig_seq_blend_factor
                
                #print("frame_count ", self.orig_seq_frame_count, " orig_seq s ", orig_seq.shape, " cur_seq s ", cur_seq.shape, " blend_factor s ", blend_factor.shape)
                
                blend_seq = cur_seq * blend_factor + orig_seq * (1.0 - blend_factor)
                #blend_seq = slerp(cur_seq, orig_seq, blend_factor)
                blend_seq = blend_seq.reshape(-1, self.joint_count, self.joint_dim)
                
                blend_seq = torch.concat( (blend_seq, self.motion_seq[:self.seq_length - self.orig_seq_frame_count, ...]), dim=0)
                
                self.motion_seq  = blend_seq
                
            else:
                
                orig_seq = orig_seq.reshape(-1, self.joint_dim)
                cur_seq  = self.motion_seq.reshape(-1, self.joint_dim)
                blend_factor = torch.ones([orig_seq.shape[0]], dtype=torch.float32).to(self.device) * self.orig_seq_blend_factor
                
                #blend_seq = slerp(cur_seq, orig_seq, blend_factor)
                blend_seq = cur_seq * blend_factor + orig_seq * (1.0 - blend_factor)
                blend_seq = blend_seq.reshape(-1, self.joint_count, self.joint_dim)
                
                self.motion_seq  = blend_seq

        self.orig_seq_changed = False
        
    def setJointPosition(self, joint_index, joint_pos, frame_count):
        
        #print("setJointPosition index ", joint_index, " rot ", joint_pos, " fraemcount ",frame_count)
        
        joint_pos = torch.from_numpy(joint_pos).to(self.device)
        
        #print("joint_pos ", joint_pos)
        
        if frame_count == 1:
            self.motion_seq[-2, joint_index, :] = joint_pos
        elif frame_count > 1:
            frame_count = min(frame_count, self.seq_length)
            joint_pos = torch.unsqueeze(joint_pos, dim=0).repeat(frame_count, 1)
            self.motion_seq[:frame_count, joint_index, :] = joint_pos

    def changeJointPosition(self, joint_index, joint_pos, frame_count):
        
        print("changeJointPosition index ", joint_index, " rot ", joint_pos, " frame_count ",frame_count)
        
        joint_pos = torch.from_numpy(joint_pos).to(torch.float32).to(self.device)
        
        #print("joint_pos ", joint_pos, " frame_count ", frame_count)
        
        #print("joint_pos s ", joint_pos.shape)
        
        if frame_count == 1:
            self.motion_seq[-2, joint_index, :] = self.motion_seq[-1, joint_index, :] + joint_pos
        elif frame_count > 1:
            frame_count = min(frame_count, self.seq_length)
            joint_pos = torch.unsqueeze(joint_pos, dim=0).repeat(frame_count, 1)
            self.motion_seq[:frame_count, joint_index, :] += joint_pos               
    
    def update(self):
        
        if self.orig_seq_changed == True:
            self.changeSequence()
            
        self.model.eval()
        
        with torch.no_grad():
            
            input_seq = torch.unsqueeze(self.motion_seq.reshape(-1, self.pose_dim), axis=0)

            input_seq_norm = (input_seq - self.pose_mean ) / self.pose_std
            input_seq_norm = torch.nan_to_num(input_seq_norm)
            
            pred_pose_norm = self.model(input_seq_norm)
            
            self.pred_pose = pred_pose_norm * self.pose_std + self.pose_mean
                
        self.pred_pose = torch.squeeze(self.pred_pose)
        self.pred_pose = self.pred_pose.reshape((1, self.joint_count, self.joint_dim))
        self.synth_pose_wpos = self.pred_pose.detach().cpu().numpy()
    
        # append pred pose to sequence
        self.motion_seq = torch.cat([self.motion_seq[1:,:], self.pred_pose], axis=0)
        
        self.model.train()
    
    