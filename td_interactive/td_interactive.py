
# -------------------------------------------------------------------------------------------------
# Motion Continuation Model - Inference Script
# Employs a Transformer Decoder MDN Architecture
# Can be trained on 6D joint rotation representations and optionally on the root joint trajectory
# -------------------------------------------------------------------------------------------------

# -------------------------------------------------------------------------------------------------
# Imports
# -------------------------------------------------------------------------------------------------

import motion_model
import motion_synthesis
import motion_sender
import motion_gui
import motion_control
from common.rotation_utils_numpy import  RotationUtilsNumpy as rot_np
from common.rotation_utils_torch import  RotationUtilsTorch as rot_to

import torch
import os, sys
import numpy as np

from common import bvh_tools as bvh
from common import fbx_tools as fbx
from common import mocap_tools as mocap

# -------------------------------------------------------------------------------------------------
# Compute Unit
# -------------------------------------------------------------------------------------------------

device = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Using {} device'.format(device))

# -------------------------------------------------------------------------------------------------
# Mocap Settings
# -------------------------------------------------------------------------------------------------

mocap_file_path = "E:/Data/mocap/stocos/Solos/Canal_14-08-2023/fbx_50hz/"
mocap_files = ["Muriel_Embodied_Machine_variation.fbx"]
mocap_pos_scale = 1.0
mocap_fps = 50
train_root_trajectory = True

# -------------------------------------------------------------------------------------------------
# Model Settings
# -------------------------------------------------------------------------------------------------

mdn_num_mixtures = 20
decoder_layer_count = 6
decoder_head_count = 8
decoder_embed_dim = 512
decoder_ff_dim = 2048
decoder_dropout = 0.1
sequence_length = 64

# -------------------------------------------------------------------------------------------------
# Training Settings
# -------------------------------------------------------------------------------------------------

decoder_weights_file = "../td/results_mdn_6d_v3/weights/decoder_weights_epoch_200"

# -------------------------------------------------------------------------------------------------
# Motion Synthesis Settings
# -------------------------------------------------------------------------------------------------

gaussian_temp = 0.1
pi_temperature = 2.0
top_p = 0.9
motion_smooth_cutoff = 6.0
motion_smooth_beta = 0.01

# -------------------------------------------------------------------------------------------------
# OSC Settings
# -------------------------------------------------------------------------------------------------

osc_send_ip = "127.0.0.1"
osc_send_port = 9004
osc_receive_ip = "0.0.0.0"
osc_receive_port = 9002

# -------------------------------------------------------------------------------------------------
# Load Mocap Data
# -------------------------------------------------------------------------------------------------

bvh_tools = bvh.BVH_Tools()
fbx_tools = fbx.FBX_Tools()
mocap_tools = mocap.Mocap_Tools()

all_mocap_data = []

for mocap_file in mocap_files:
    print("process file ", mocap_file)
    if mocap_file.endswith(".bvh") or mocap_file.endswith(".BVH"):
        bvh_data = bvh_tools.load(os.path.join(mocap_file_path, mocap_file))
        mocap_data = mocap_tools.bvh_to_mocap(bvh_data)
        mocap_data["motion"]["rot_local"] = mocap_tools.euler_to_quat_bvh(mocap_data["motion"]["rot_local_euler"], mocap_data["rot_sequence"])
    elif mocap_file.endswith(".fbx") or mocap_file.endswith(".FBX"):
        fbx_data = fbx_tools.load(os.path.join(mocap_file_path, mocap_file))
        mocap_data = mocap_tools.fbx_to_mocap(fbx_data)[0] 
        mocap_data["motion"]["rot_local"] = mocap_tools.euler_to_quat(mocap_data["motion"]["rot_local_euler"], mocap_data["rot_sequence"])

    mocap_data["skeleton"]["offsets"] *= mocap_pos_scale
    mocap_data["motion"]["pos_local"] *= mocap_pos_scale

    if not train_root_trajectory:
        mocap_data["skeleton"]["offsets"][0, 0] = 0.0 
        mocap_data["skeleton"]["offsets"][0, 2] = 0.0 
        mocap_data["motion"]["pos_local"][:, 0, 0] = 0.0
        mocap_data["motion"]["pos_local"][:, 0, 2] = 0.0

    all_mocap_data.append(mocap_data)

all_pose_sequences = []

# Prepare sequences for synthesis in 6D + Trajectory format
for mocap_data in all_mocap_data:
    pose_sequence = mocap_data["motion"]["rot_local"]
    pose_sequence_6d = rot_np.quat_to_r6d(pose_sequence)
    joint_count = pose_sequence_6d.shape[1]
    joint_dim = 6
    pose_dim = joint_count * joint_dim
    pose_sequence_6d_flat = np.reshape(pose_sequence_6d, (-1, pose_dim))

    if train_root_trajectory:
        root_positions = mocap_data["motion"]["pos_local"][:, 0, :]
        pose_sequence_input = np.concatenate((root_positions, pose_sequence_6d_flat), axis=1)
    else:
        pose_sequence_input = pose_sequence_6d_flat

    all_pose_sequences.append(pose_sequence_input.astype(np.float32))

input_dim = all_pose_sequences[0].shape[1]

# Assuming root_pos_mean and std are computed somewhere or saved with the model.
# For simplicity, we calculate it from the dataset here.
all_excerpts = np.concatenate(all_pose_sequences, axis=0)
if train_root_trajectory:
    root_pos_mean = np.mean(all_excerpts[:, :3], axis=0, keepdims=True)
    root_pos_std = np.std(all_excerpts[:, :3], axis=0, keepdims=True)
    root_pos_std[root_pos_std == 0] = 1.0

# -------------------------------------------------------------------------------------------------
# Setup Model
# -------------------------------------------------------------------------------------------------

motion_model.config["input_dim"] = input_dim
motion_model.config["embed_dim"] = decoder_embed_dim
motion_model.config["num_heads"] = decoder_head_count
motion_model.config["num_layers"] = decoder_layer_count
motion_model.config["ff_dim"] = decoder_ff_dim
motion_model.config["dropout"] = decoder_dropout
motion_model.config["pos_encoding_max_length"] = sequence_length
motion_model.config["num_mixtures"] = mdn_num_mixtures
motion_model.config["device"] = device
motion_model.config["weights_path"] = decoder_weights_file

model = motion_model.createModel(motion_model.config) 

# -------------------------------------------------------------------------------------------------
# Setup Motion Synthesis
# -------------------------------------------------------------------------------------------------

synthesis_config = motion_synthesis.config
synthesis_config["skeleton"] = all_mocap_data[0]["skeleton"]
synthesis_config["model"] = model
synthesis_config["seq_length"] = sequence_length
synthesis_config["orig_sequences"] = all_pose_sequences
synthesis_config["orig_seq_index"] = 0
synthesis_config["device"] = device 
synthesis_config["train_root_trajectory"] = train_root_trajectory
synthesis_config["root_pos_mean"] = root_pos_mean if train_root_trajectory else None
synthesis_config["root_pos_std"] = root_pos_std if train_root_trajectory else None
synthesis_config["joint_count"] = joint_count
synthesis_config["smooth_freq"] = mocap_fps
synthesis_config["smooth_cutoff"] = motion_smooth_cutoff
synthesis_config["smooth_beta"] = motion_smooth_beta
synthesis_config["gaussian_temp"] = gaussian_temp
synthesis_config["pi_temperature"] = pi_temperature
synthesis_config["top_p"] = top_p

synthesis = motion_synthesis.MotionSynthesis(synthesis_config)

# -------------------------------------------------------------------------------------------------
# Setup OSC Sender
# -------------------------------------------------------------------------------------------------

motion_sender.config["ip"] = osc_send_ip
motion_sender.config["port"] = osc_send_port
osc_sender = motion_sender.OscSender(motion_sender.config)

# -------------------------------------------------------------------------------------------------
# Setup GUI
# -------------------------------------------------------------------------------------------------

from PyQt5 import QtWidgets
motion_gui.config["synthesis"] = synthesis
motion_gui.config["sender"] = osc_sender
motion_gui.config["update_interval"] = 1.0 / mocap_fps

app = QtWidgets.QApplication(sys.argv)
gui = motion_gui.MotionGui(motion_gui.config)

def closeEvent():
    QtWidgets.QApplication.quit()
app.lastWindowClosed.connect(closeEvent)

# -------------------------------------------------------------------------------------------------
# Setup OSC Control
# -------------------------------------------------------------------------------------------------

motion_control.config["motion_seq"] = pose_sequence
motion_control.config["synthesis"] = synthesis
motion_control.config["gui"] = gui
motion_control.config["ip"] = osc_receive_ip
motion_control.config["port"] = osc_receive_port

osc_control = motion_control.MotionControl(motion_control.config)

# -------------------------------------------------------------------------------------------------
# Start Application
# -------------------------------------------------------------------------------------------------

osc_control.start()
gui.show()
app.exec_()
osc_control.stop()
