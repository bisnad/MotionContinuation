"""
Motion Continuation (Transformer Decoder Version)
"""

"""
Imports
"""

import torch
import torch.nn as nn
import torch.nn.functional as nnF
import torch.optim as optim
from torch.utils.data import Dataset
from torch.utils.data import DataLoader
import torchaudio
import torchaudio.transforms as transforms
from collections import OrderedDict

import numpy as np
import math
import time
import csv
import json
import os
import matplotlib.pyplot as plt


from common import utils
from common import bvh_tools as bvh
from common import fbx_tools as fbx
from common import mocap_tools as mocap
from common.quaternion import qmul, qrot, qnormalize_np, slerp
from common.pose_renderer import PoseRenderer

"""
Settings
"""

"""
Compute Device
"""

device = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Using {} device'.format(device))

"""
Mocap Settings
"""

mocap_file_path = "E:/Data/mocap/stocos/Solos/Canal_14-08-2023/fbx_50hz/"
mocap_files = ["Muriel_Embodied_Machine_variation.fbx"]
mocap_valid_frame_ranges = [ [ [ 200, 6400 ] ] ]
mocap_pos_scale = 1.0
mocap_fps = 50
mocap_loss_weights_file = None

"""
Model Settings
"""

decoder_layer_count = 6
decoder_head_count = 8
decoder_embed_dim = 512
decoder_ff_dim = 2048
decoder_dropout = 0.1

save_weights = True
load_weights = False
decoder_weights_file = "results/weights/decoder_weights_epoch_200"

"""
Training settings
"""

batch_size = 32
test_percentage = 0.1

seq_input_length = 64
seq_output_length = 10 # this is only used for non-teacher forcing scenarios
seq_offset = 4

learning_rate = 1e-4
norm_loss_scale = 0.1
pos_loss_scale = 0.1
rot_loss_scale = 0.9
teacher_forcing_prob = 0.0
model_save_interval = 10

epochs = 200
save_history = True

"""
Visualization settings
"""

view_ele = 0.0
view_azi = 0.0
view_line_width = 1.0
view_size = 4.0

"""
Load Mocap Data
"""

bvh_tools = bvh.BVH_Tools()
fbx_tools = fbx.FBX_Tools()
mocap_tools = mocap.Mocap_Tools()

all_mocap_data = []

for mocap_file in mocap_files:
    
    print("process file ", mocap_file)
    
    if mocap_file.endswith(".bvh") or mocap_file.endswith(".BVH"):
        bvh_data = bvh_tools.load(mocap_file_path + "/" + mocap_file)
        mocap_data = mocap_tools.bvh_to_mocap(bvh_data)
    elif mocap_file.endswith(".fbx") or mocap_file.endswith(".FBX"):
        fbx_data = fbx_tools.load(mocap_file_path + "/" + mocap_file)
        mocap_data = mocap_tools.fbx_to_mocap(fbx_data)[0] # first skeleton only
    
    mocap_data["skeleton"]["offsets"] *= mocap_pos_scale
    mocap_data["motion"]["pos_local"] *= mocap_pos_scale
    
    # set x and z offset of root joint to zero
    mocap_data["skeleton"]["offsets"][0, 0] = 0.0 
    mocap_data["skeleton"]["offsets"][0, 2] = 0.0 
    
    if mocap_file.endswith(".bvh") or mocap_file.endswith(".BVH"):
        mocap_data["motion"]["rot_local"] = mocap_tools.euler_to_quat_bvh(mocap_data["motion"]["rot_local_euler"], mocap_data["rot_sequence"])
    elif mocap_file.endswith(".fbx") or mocap_file.endswith(".FBX"):
        mocap_data["motion"]["rot_local"] = mocap_tools.euler_to_quat(mocap_data["motion"]["rot_local_euler"], mocap_data["rot_sequence"])

    all_mocap_data.append(mocap_data)


# retrieve mocap properties

mocap_data = all_mocap_data[0]
joint_count = mocap_data["motion"]["rot_local"].shape[1]
joint_dim = mocap_data["motion"]["rot_local"].shape[2]
pose_dim = joint_count * joint_dim

offsets = mocap_data["skeleton"]["offsets"].astype(np.float32)
parents = mocap_data["skeleton"]["parents"]
children = mocap_data["skeleton"]["children"]

# create edge list
def get_edge_list(children):
    edge_list = []

    for parent_joint_index in range(len(children)):
        for child_joint_index in children[parent_joint_index]:
            edge_list.append([parent_joint_index, child_joint_index])
    
    return edge_list

edge_list = get_edge_list(children)

# set joint loss weigths 

if mocap_loss_weights_file is not None:
    with open(mocap_loss_weights_file) as f:
        joint_loss_weights = json.load(f)
        joint_loss_weights = joint_loss_weights["joint_loss_weights"]
else:
    joint_loss_weights = [1.0]
    joint_loss_weights *= joint_count

    
"""
Create Dataset
"""

X = []
y = []

for i, mocap_data in enumerate(all_mocap_data):
    
    print("mocap ", mocap_files[i])
    
    pose_sequence = mocap_data["motion"]["rot_local"]
    pose_sequence = np.reshape(pose_sequence, (-1, pose_dim))
    
    print("shape ", pose_sequence.shape)
    
    valid_frame_ranges = mocap_valid_frame_ranges[i]
    
    for valid_frame_range in valid_frame_ranges:
        
        frame_range_start = valid_frame_range[0]
        frame_range_end = valid_frame_range[1]
        
        print("frame range from ", frame_range_start, " to ", frame_range_end)
        
        for pI in np.arange(frame_range_start, frame_range_end - seq_input_length - seq_output_length - 1, seq_offset):

            X_sample = pose_sequence[pI:pI+seq_input_length]
            X.append(X_sample.reshape((seq_input_length, pose_dim)))
            
            Y_sample = pose_sequence[pI+seq_input_length:pI+seq_input_length+seq_output_length]  
            y.append(Y_sample.reshape((seq_output_length, pose_dim)))

X = np.array(X)
y = np.array(y)

X = torch.from_numpy(X).to(torch.float32)
y = torch.from_numpy(y).to(torch.float32)

class SequenceDataset(Dataset):
    def __init__(self, X, y):
        self.X = X
        self.y = y
    
    def __len__(self):
        return self.X.shape[0]
    
    def __getitem__(self, idx):
        return self.X[idx, ...], self.y[idx, ...]

full_dataset = SequenceDataset(X, y)

print("full_dataset len ", len(full_dataset))

X_item, y_item = full_dataset[0]

print("X_item s ", X_item.shape)
print("y_item s ", y_item.shape)

test_size = int(test_percentage * len(full_dataset))
train_size = len(full_dataset) - test_size

train_dataset, test_dataset = torch.utils.data.random_split(full_dataset, [train_size, test_size])

train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False)

X_batch, y_batch = next(iter(train_loader))

print("X_batch s ", X_batch.shape)
print("y_batch s ", y_batch.shape)

"""
Create Models
"""

"""
PositionalEncoding
"""

class PositionalEncoding(nn.Module):
    def __init__(self, dim_model, dropout_p, max_len):
        super().__init__()
        self.dropout = nn.Dropout(dropout_p)

        pos_encoding = torch.zeros(max_len, dim_model)
        positions_list = torch.arange(0, max_len, dtype=torch.float).view(-1, 1)
        division_term = torch.exp(
            torch.arange(0, dim_model, 2).float() * (-math.log(10000.0)) / dim_model
        )
        pos_encoding[:, 0::2] = torch.sin(positions_list * division_term)
        pos_encoding[:, 1::2] = torch.cos(positions_list * division_term)

        # for batch-first: [1, max_len, dim_model]
        pos_encoding = pos_encoding.unsqueeze(0)
        self.register_buffer("pos_encoding", pos_encoding)
        
    def forward(self, token_embedding: torch.tensor) -> torch.tensor:
        
        #print("token_embedding s ", token_embedding.shape)
        #print("pos_encoding s ", self.pos_encoding.shape)
        
        # token_embedding: [batch_size, seq_len, dim_model]
        seq_len = token_embedding.size(1)
        # broadcast over batch dimension
        pe = self.pos_encoding[:, :seq_len, :]
        
        return self.dropout(token_embedding + pe)

"""
Create TransformerDecoder
"""

class TransformerDecoder(nn.Module):
    def __init__(
        self,
        motion_dim,
        embed_dim,
        num_heads,
        num_decoder_layers,
        ff_dim,
        dropout_p,
        pos_encoding_max_length
    ):
        super().__init__()
        self.embed_dim = embed_dim
        self.motion2embed = nn.Linear(motion_dim, embed_dim)
        
        # Increased max_len for pos_encoding to handle input length + output length during teacher forcing
        self.positional_encoder = PositionalEncoding(
            dim_model=embed_dim, dropout_p=dropout_p, max_len=pos_encoding_max_length * 2 
        )

        # Replace TransformerDecoderLayer with TransformerEncoderLayer
        self.encoder_layer = nn.TransformerEncoderLayer(
            d_model=embed_dim, 
            nhead=num_heads, 
            dim_feedforward=ff_dim,
            dropout=dropout_p, 
            activation='gelu', 
            batch_first=True
        )
        
        self.transformer = nn.TransformerEncoder(self.encoder_layer, num_layers=num_decoder_layers)
        self.embed2motion = nn.Linear(embed_dim, motion_dim) 

    def forward(self, motion_data, return_sequence=False):
        seq_len = motion_data.size(1)

        # Use PyTorch's optimized causal mask
        tgt_mask = nn.Transformer.generate_square_subsequent_mask(seq_len, device=motion_data.device)

        motion_embedded = self.motion2embed(motion_data) * math.sqrt(self.embed_dim)
        motion_embedded = self.positional_encoder(motion_embedded)

        # Pass through the Transformer Encoder (acting as a decoder without cross-attention)
        decoder_out = self.transformer(motion_embedded, mask=tgt_mask, is_causal=True)
        out = self.embed2motion(decoder_out)

        # If return_sequence is True, return all steps for vectorized loss computation
        if return_sequence:
            return out
        else:
            return out[:, -1, :] # standard autoregressive generation

decoder = TransformerDecoder(motion_dim=pose_dim,
                          embed_dim=decoder_embed_dim, 
                          num_heads=decoder_head_count, 
                          num_decoder_layers=decoder_layer_count, 
                          ff_dim = decoder_ff_dim,
                          dropout_p=decoder_dropout,
                          pos_encoding_max_length=seq_input_length).to(device)


print(decoder)

if load_weights == True:
    decoder.load_state_dict(torch.load(decoder_weights_file))

# test transformer decoder
x_batch, _ = next(iter(train_loader))

decoder_input = x_batch.to(device)
decoder_output = decoder(decoder_input)

print("decoder_input s ", decoder_input.shape)
print("decoder_output s ", decoder_output.shape)

"""
Training
"""

"""
Create Optimizer and Scheduler
"""

optimizer = torch.optim.Adam(decoder.parameters(), lr=learning_rate)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=50, gamma=0.336) # reduce the learning every 20 epochs by a factor of 10

"""
Create Loss Functions
"""

joint_loss_weights = torch.tensor(joint_loss_weights, dtype=torch.float32)
joint_loss_weights = joint_loss_weights.reshape(1, 1, -1).to(device)

def norm_loss(yhat):
    _yhat = yhat.view(-1, 4)
    _norm = torch.norm(_yhat, dim=1)
    _diff = (_norm - 1.0) ** 2
    _loss = torch.mean(_diff)
    return _loss

def forward_kinematics(rotations, root_positions):
    """
    Perform forward kinematics using the given trajectory and local rotations.
    Arguments (where N = batch size, L = sequence length, J = number of joints):
     -- rotations: (N, L, J, 4) tensor of unit quaternions describing the local rotations of each joint.
     -- root_positions: (N, L, 3) tensor describing the root joint positions.
    """

    assert len(rotations.shape) == 4
    assert rotations.shape[-1] == 4
    
    toffsets = torch.tensor(offsets).to(device)
    
    positions_world = []
    rotations_world = []

    expanded_offsets = toffsets.expand(rotations.shape[0], rotations.shape[1], offsets.shape[0], offsets.shape[1])

    # Parallelize along the batch and time dimensions
    for jI in range(offsets.shape[0]):
        if parents[jI] == -1:
            positions_world.append(root_positions)
            rotations_world.append(rotations[:, :, 0])
        else:
            positions_world.append(qrot(rotations_world[parents[jI]], expanded_offsets[:, :, jI]) \
                                   + positions_world[parents[jI]])
            if len(children[jI]) > 0:
                rotations_world.append(qmul(rotations_world[parents[jI]], rotations[:, :, jI]))
            else:
                # This joint is a terminal node -> it would be useless to compute the transformation
                rotations_world.append(None)

    return torch.stack(positions_world, dim=3).permute(0, 1, 3, 2)

def pos_loss(y, yhat):
    
    #print("pos_loss")
    #print("y s ", y.shape)
    #print("yhat s ", yhat.shape)
    
    # y and yhat shapes: batch_size, seq_length, pose_dim

    # normalize tensors
    _yhat = yhat.view(-1, 4)

    _yhat_norm = nn.functional.normalize(_yhat, p=2, dim=1)
    _y_rot = y.view((y.shape[0], y.shape[1], -1, 4))
    _yhat_rot = _yhat_norm.view((y.shape[0], y.shape[1], -1, 4))
    
    #print("_y_rot s ", _y_rot.shape)
    #print("_yhat_rot s ", _yhat_rot.shape)

    zero_trajectory = torch.zeros((y.shape[0], y.shape[1], 3), dtype=torch.float32, requires_grad=True).to(device)

    _y_pos = forward_kinematics(_y_rot, zero_trajectory)
    _yhat_pos = forward_kinematics(_yhat_rot, zero_trajectory)
    
    #print("_y_pos s ", _y_pos.shape)
    #print("_yhat_pos s ", _yhat_pos.shape)

    _pos_diff = torch.norm((_y_pos - _yhat_pos), dim=3)
    
    #print("_pos_diff s ", _pos_diff.shape)
    
    _pos_diff_weighted = _pos_diff * joint_loss_weights
    
    _loss = torch.mean(_pos_diff_weighted)

    return _loss

def rot_loss(y, yhat):
    
    #print("rot_loss")
    #print("y s ", y.shape)
    #print("yhat s ", yhat.shape)
    
    # y and yhat shapes: batch_size, seq_length, pose_dim
    
    # normalize quaternion
    
    _y = y.view((-1, 4))
    _yhat = yhat.view((-1, 4))
    _yhat_norm = nn.functional.normalize(_yhat, p=2, dim=1)
    
    #print("_y s ", _y.shape)
    #print("_yhat_norm s ", _yhat_norm.shape)
    
    # inverse of quaternion: https://www.mathworks.com/help/aeroblks/quaternioninverse.html
    _yhat_inv = _yhat_norm * torch.tensor([[1.0, -1.0, -1.0, -1.0]], dtype=torch.float32).to(device)

    # calculate difference quaternion
    _diff = qmul(_yhat_inv, _y)
    # length of complex part
    _len = torch.norm(_diff[:, 1:], dim=1)
    # atan2
    _atan = torch.atan2(_len, _diff[:, 0])
    # abs
    _abs = torch.abs(_atan)
    
    _abs = _abs.reshape(-1, 1, joint_count)
    
    #print("_abs s ", _abs.shape)
    
    _abs_weighted = _abs * joint_loss_weights
    
    _loss = torch.mean(_abs_weighted)   
    return _loss

# autoencoder loss function
def loss(y, yhat):
    _norm_loss = norm_loss(yhat)
    _pos_loss = pos_loss(y, yhat)
    _rot_loss = rot_loss(y, yhat)
    
    _total_loss = 0.0
    _total_loss += _norm_loss * norm_loss_scale
    _total_loss += _pos_loss * pos_loss_scale
    _total_loss += _rot_loss * rot_loss_scale
    
    return _total_loss, _norm_loss, _pos_loss, _rot_loss


def train_step(pose_sequences, target_poses, teacher_forcing):
    decoder.train()

    output_poses_length = target_poses.shape[1]

    if teacher_forcing:
        # Vectorized Teacher Forcing:
        # Predict the entire target sequence in parallel by prepending the input
        # and letting the causal mask handle attention causality.
        _target_inputs = target_poses[:, :-1, :]
        _full_input = torch.cat((pose_sequences, _target_inputs), dim=1)

        _full_preds = decoder(_full_input, return_sequence=True)

        # Extract only the temporal predictions that map to the target block
        _pred_poses_for_loss = _full_preds[:, -output_poses_length:, :]
        _target_poses_for_loss = target_poses
    else:
        # Autoregressive generation (step-by-step)
        _input_poses = pose_sequences 
        _pred_poses_list = []
        
        for o_i in range(output_poses_length):
            _pred_pose = decoder(_input_poses, return_sequence=False)
            _pred_pose = torch.unsqueeze(_pred_pose, axis=1)
            
            _pred_poses_list.append(_pred_pose)

            # Shift the input window 
            _input_poses = _input_poses[:, 1:, :].detach().clone()
            _input_poses = torch.cat((_input_poses, _pred_pose.detach().clone()), axis=1)

        _pred_poses_for_loss = torch.cat(_pred_poses_list, dim=1)
        _target_poses_for_loss = target_poses

    _loss, _norm_loss, _pos_loss, _rot_loss = loss(_target_poses_for_loss, _pred_poses_for_loss) 

    optimizer.zero_grad()
    _loss.backward()
    optimizer.step()

    return _loss, _norm_loss, _pos_loss, _rot_loss

@torch.no_grad()
def test_step(pose_sequences, target_poses, teacher_forcing):
    decoder.eval() # <-- BUG FIX: Was decoder.train()

    output_poses_length = target_poses.shape[1]

    if teacher_forcing:
        _target_inputs = target_poses[:, :-1, :]
        _full_input = torch.cat((pose_sequences, _target_inputs), dim=1)
        _full_preds = decoder(_full_input, return_sequence=True)
        
        _pred_poses_for_loss = _full_preds[:, -output_poses_length:, :]
        _target_poses_for_loss = target_poses
    else:
        _input_poses = pose_sequences 
        _pred_poses_list = []
        
        for o_i in range(output_poses_length):
            _pred_pose = decoder(_input_poses, return_sequence=False)
            _pred_pose = torch.unsqueeze(_pred_pose, axis=1)
            _pred_poses_list.append(_pred_pose)

            _input_poses = _input_poses[:, 1:, :]
            _input_poses = torch.cat((_input_poses, _pred_pose), axis=1)

        _pred_poses_for_loss = torch.cat(_pred_poses_list, dim=1)
        _target_poses_for_loss = target_poses

    _loss, _norm_loss, _pos_loss, _rot_loss = loss(_target_poses_for_loss, _pred_poses_for_loss) 
    return _loss, _norm_loss, _pos_loss, _rot_loss

def train(train_dataloader, test_dataloader, epochs):
    
    loss_history = {}
    loss_history["train"] = []
    loss_history["test"] = []
    loss_history["norm"] = []
    loss_history["pos"] = []
    loss_history["rot"] = []

    for epoch in range(epochs):
        start = time.time()
        
        _train_loss_per_epoch = []
        _norm_loss_per_epoch = []
        _pos_loss_per_epoch = []
        _rot_loss_per_epoch = []

        for train_batch in train_dataloader:
            input_pose_sequences = train_batch[0].to(device)
            target_poses = train_batch[1].to(device)
            
            use_teacher_forcing = np.random.uniform() < teacher_forcing_prob
            
            _loss, _norm_loss, _pos_loss, _rot_loss = train_step(input_pose_sequences, target_poses, use_teacher_forcing)
            
            _loss = _loss.detach().cpu().numpy()
            _norm_loss = _norm_loss.detach().cpu().numpy()
            _pos_loss = _pos_loss.detach().cpu().numpy()
            _rot_loss = _rot_loss.detach().cpu().numpy()
            
            _train_loss_per_epoch.append(_loss)
            _norm_loss_per_epoch.append(_norm_loss)
            _pos_loss_per_epoch.append(_pos_loss)
            _rot_loss_per_epoch.append(_rot_loss)

        _train_loss_per_epoch = np.mean(np.array(_train_loss_per_epoch))
        _norm_loss_per_epoch = np.mean(np.array(_norm_loss_per_epoch))
        _pos_loss_per_epoch = np.mean(np.array(_pos_loss_per_epoch))
        _rot_loss_per_epoch = np.mean(np.array(_rot_loss_per_epoch))

        _test_loss_per_epoch = []
        
        for test_batch in test_dataloader:
            input_pose_sequences = test_batch[0].to(device)
            target_poses = test_batch[1].to(device)
            
            use_teacher_forcing = np.random.uniform() < teacher_forcing_prob
            
            _loss, _, _, _ = test_step(input_pose_sequences, target_poses, use_teacher_forcing)
            #_loss, _, _ = test_step(input_pose_sequences, target_poses)
            
            _loss = _loss.detach().cpu().numpy()
            
            _test_loss_per_epoch.append(_loss)
        
        _test_loss_per_epoch = np.mean(np.array(_test_loss_per_epoch))
        
        if epoch % model_save_interval == 0 and save_weights == True:
            torch.save(decoder.state_dict(), "results/weights/decoder_weights_epoch_{}".format(epoch))
        
        loss_history["train"].append(_train_loss_per_epoch)
        loss_history["test"].append(_test_loss_per_epoch)
        loss_history["norm"].append(_norm_loss_per_epoch)
        loss_history["pos"].append(_pos_loss_per_epoch)
        loss_history["rot"].append(_rot_loss_per_epoch)
        
        scheduler.step()
        
        print ('epoch {} : train: {:01.4f} test: {:01.4f} norm {:01.4f} pos {:01.4f} rot {:01.4f} time {:01.2f}'.format(epoch + 1, _train_loss_per_epoch, _test_loss_per_epoch, _norm_loss_per_epoch, _pos_loss_per_epoch, _rot_loss_per_epoch, time.time()-start))
    
    return loss_history

"""
Execute Training
"""

loss_history = train(train_loader, test_loader, epochs)

"""
Save Training History
"""

utils.save_loss_as_csv(loss_history, "results/histories/rnn_history_{}.csv".format(epochs))
utils.save_loss_as_image(loss_history, "results/histories/rnn_history_{}.png".format(epochs))

"""
Save Final Model Weights
"""

torch.save(decoder.state_dict(), "results/weights/decoder_weights_epoch_{}".format(epochs))

"""
Inference
"""

decoder.eval()

poseRenderer = PoseRenderer(edge_list)

def export_sequence_anim(pose_sequence, file_name):
    
    pose_count = pose_sequence.shape[0]
    pose_sequence = np.reshape(pose_sequence, (pose_count, joint_count, joint_dim))
    
    pose_sequence = torch.tensor(np.expand_dims(pose_sequence, axis=0)).to(device)
    zero_trajectory = torch.tensor(np.zeros((1, pose_count, 3), dtype=np.float32)).to(device)
    
    skel_sequence = forward_kinematics(pose_sequence, zero_trajectory)
    
    skel_sequence = skel_sequence.detach().cpu().numpy()
    skel_sequence = np.squeeze(skel_sequence)    
    
    view_min, view_max = utils.get_equal_mix_max_positions(skel_sequence)
    skel_images = poseRenderer.create_pose_images(skel_sequence, view_min, view_max, view_ele, view_azi, view_line_width, view_size, view_size)
    skel_images[0].save(file_name, save_all=True, append_images=skel_images[1:], optimize=False, duration=33.0, loop=0)

def export_sequence_bvh(pose_sequence, file_name):
    
    pose_count = pose_sequence.shape[0]

    pred_dataset = {}
    pred_dataset["frame_rate"] = mocap_data["frame_rate"]
    pred_dataset["rot_sequence"] = mocap_data["rot_sequence"]
    pred_dataset["skeleton"] = mocap_data["skeleton"]
    pred_dataset["motion"] = {}
    pred_dataset["motion"]["pos_local"] = np.repeat(np.expand_dims(pred_dataset["skeleton"]["offsets"], axis=0), pose_count, axis=0)
    pred_dataset["motion"]["rot_local"] = pose_sequence
    pred_dataset["motion"]["rot_local_euler"] = mocap_tools.quat_to_euler_bvh(pred_dataset["motion"]["rot_local"], pred_dataset["rot_sequence"])

    pred_bvh = mocap_tools.mocap_to_bvh(pred_dataset)
    
    bvh_tools.write(pred_bvh, file_name)

def export_sequence_fbx(pose_sequence, file_name):
    
    pose_count = pose_sequence.shape[0]
    
    pred_dataset = {}
    pred_dataset["frame_rate"] = mocap_data["frame_rate"]
    pred_dataset["rot_sequence"] = mocap_data["rot_sequence"]
    pred_dataset["skeleton"] = mocap_data["skeleton"]
    pred_dataset["motion"] = {}
    pred_dataset["motion"]["pos_local"] = np.repeat(np.expand_dims(pred_dataset["skeleton"]["offsets"], axis=0), pose_count, axis=0)
    pred_dataset["motion"]["rot_local"] = pose_sequence
    pred_dataset["motion"]["rot_local_euler"] = mocap_tools.quat_to_euler(pred_dataset["motion"]["rot_local"], pred_dataset["rot_sequence"])
    
    pred_fbx = mocap_tools.mocap_to_fbx([pred_dataset])
    
    fbx_tools.write(pred_fbx, file_name)
    
"""
Motion Continuation Functions
"""

@torch.no_grad()
def create_pred_sequence(pose_sequence, pose_count):
    
    start_seq = pose_sequence
    start_seq = torch.from_numpy(start_seq).to(device)
    start_seq = torch.reshape(start_seq, (seq_input_length, pose_dim))
    
    next_seq = start_seq
    
    pred_poses = []
    
    for i in range(pose_count):
        
        with torch.no_grad():
            pred_pose = decoder(torch.unsqueeze(next_seq, axis=0))

        # normalize pred pose
        pred_pose = torch.squeeze(pred_pose)
        pred_pose = pred_pose.reshape((-1, 4))
        pred_pose = nn.functional.normalize(pred_pose, p=2, dim=1)
        pred_pose = pred_pose.reshape((1, pose_dim))

        pred_poses.append(pred_pose)
    
        #print("next_seq s ", next_seq.shape)
        #print("pred_pose s ", pred_pose.shape)

        next_seq = torch.cat([next_seq[1:,:], pred_pose], axis=0)

    pred_poses = torch.cat(pred_poses, dim=0)
    pred_poses = pred_poses.reshape((pose_count, joint_count, joint_dim))

    return pred_poses.detach().cpu().numpy()

"""
Perform Motion Continuation
"""

seq_index = 0
seq_start = 1000
seq_length = 1000

# create original sequence

orig_sequence = all_mocap_data[seq_index]["motion"]["rot_local"].astype(np.float32)

export_sequence_anim(orig_sequence[seq_start:seq_start+seq_length], "results/anims/orig_sequence_seq_start_{}_length_{}.gif".format(seq_start, seq_length))
export_sequence_fbx(orig_sequence[seq_start:seq_start+seq_length], "results/anims/orig_sequence_seq_start_{}_length_{}.fbx".format(seq_start, seq_length))
#export_sequence_bvh(orig_sequence[seq_start:seq_start+seq_length], "results/anims/orig_sequence_seq_start_{}_length_{}.bvh".format(seq_start, seq_length))

# create predicted sequence

orig_sequence = all_mocap_data[seq_index]["motion"]["rot_local"].astype(np.float32)
pred_sequence = create_pred_sequence(orig_sequence[seq_start:seq_start+seq_input_length], seq_length)

export_sequence_anim(pred_sequence, "results/anims/pred_sequence_epoch_{}_seq_start_{}_length_{}.gif".format(epochs, seq_start, seq_length))
export_sequence_fbx(pred_sequence, "results/anims/pred_sequence_epoch_{}_seq_start_{}_length_{}.fbx".format(epochs, seq_start, seq_length))
#export_sequence_bvh(pred_sequence, "results/anims/pred_sequence_epoch_{}_seq_start_{}_length_{}.bvh".format(epochs, seq_start, seq_length))





