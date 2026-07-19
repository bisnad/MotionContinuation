import pandas
import math
import numpy as np
import transforms3d as t3d
from scipy.spatial.transform import Rotation
from common import bvh_tools as bvh
from common import fbx_tools as fbx
from common.rotation_utils_numpy import RotationUtilsNumpy as rot_np
import copy
import json

class Mocap_Tools:

    def bvh_to_mocap(self, bvh_data):
        
        mocap_data = {}
        
        mocap_data["frame_rate"] = 1.0 / bvh_data.framerate
        
        rot_channels = list(bvh_data.skeleton.values())[0]["channels"][3:]
        rot_channel_names = ["Xrotation", "Yrotation", "Zrotation"]
        mocap_data["rot_sequence"]  = [ rot_channel_names.index(rot_channel) for rot_channel in rot_channels ]
        
        self._create_skeleton_data(bvh_data, mocap_data)
        self._create_motion_data(bvh_data, mocap_data)
        
        return mocap_data
    
    def fbx_to_mocap(self, fbx_data):
        
        all_motion_data = []

        for fbx_per_skel_data in fbx_data:
            
            skeleton = {}
            skeleton["root"] = fbx_per_skel_data.skeleton_root
            skeleton["joints"] = fbx_per_skel_data.skeleton_joints
            skeleton["parents"] = fbx_per_skel_data.skeleton_parents
            skeleton["children"] = fbx_per_skel_data.skeleton_children
            skeleton["offsets"] = fbx_per_skel_data.skeleton_joint_offsets
            
            motion = {}
            motion["pos_local"] = fbx_per_skel_data.motion_pos_local
            motion["rot_local_euler"] = fbx_per_skel_data.motion_rot_local_euler
            
            motion_data = {}
            motion_data["frame_rate"] = fbx_per_skel_data.motion_frame_rate 
            motion_data["rot_sequence"] = fbx_per_skel_data.motion_rot_sequence
            motion_data["skeleton"] = skeleton
            motion_data["motion"] = motion
            
            all_motion_data.append(motion_data)
    
        return all_motion_data
    
    def mocap_to_bvh(self, mocap_data):
        
        bvh_data = bvh.BVH_Data()
        bvh_data.framerate = 1.0 / mocap_data["frame_rate"]
        bvh_data.root_name = mocap_data["skeleton"]["root"]
        
        bvh_channel_names, bvh_channels = self._create_bvh_channel_names(mocap_data)
        bvh_data.channel_names = bvh_channel_names
        
        bvh_skeleton = self._create_bvh_skeleton(mocap_data, bvh_channels)
        bvh_data.skeleton = bvh_skeleton
        
        bvh_frames = self._create_bvh_frames(mocap_data, bvh_channels)
        bvh_data.values = bvh_frames
        
        return bvh_data
    
    # warning: this doesn't create any fbx nodes
    # the nodes are only created when writing the fmx_data to a file using the FBX_Tools 
    def mocap_to_fbx(self, all_motion_data):
        
        fbx_data = []
        
        for motion_per_skel_data in all_motion_data:
        
            fbx_per_skel_data = fbx.FBX_Mocap_Data()
            
            motion_skeleton = motion_per_skel_data["skeleton"]
            motion_motion = motion_per_skel_data["motion"]
            
            fbx_per_skel_data.motion_frame_rate = motion_per_skel_data["frame_rate"]
            fbx_per_skel_data.motion_rot_sequence = motion_per_skel_data["rot_sequence"]
            fbx_per_skel_data.skeleton_root = motion_skeleton["root"]
            fbx_per_skel_data.skeleton_joints = motion_skeleton["joints"]
            fbx_per_skel_data.skeleton_children = motion_skeleton["children"]
            fbx_per_skel_data.skeleton_parents = motion_skeleton["parents"]
            fbx_per_skel_data.skeleton_joint_offsets = motion_skeleton["offsets"]
            fbx_per_skel_data.motion_pos_local = motion_motion["pos_local"]
            fbx_per_skel_data.motion_rot_local_euler = motion_motion["rot_local_euler"]
            fbx_per_skel_data.motion_frame_count = fbx_per_skel_data.motion_rot_local_euler.shape[0]

            fbx_data.append(fbx_per_skel_data)
            
        return fbx_data
    
    def local_to_world(self, rot_local, pos_local, skeleton):
        
        root_name = skeleton["root"]
        joint_names = skeleton["joints"]
        root_index = joint_names.index(root_name)
        parents = skeleton["parents"]
        children = skeleton["children"]
        offsets = skeleton["offsets"]
        
        root_positions = pos_local[:, root_index, :]

        frame_count = rot_local.shape[0]
        joint_count = rot_local.shape[1]
        
        positions_world = []
        rotations_world = []
        
        for fI in range(frame_count):
            
            frame_positions_world = []
            frame_rotations_world = []
            
            for jI in range(joint_count):
                
                if parents[jI] == -1:
                    frame_positions_world.append(root_positions[fI])
                    frame_rotations_world.append(rot_local[fI, 0])
                else:
                    frame_positions_world.append(t3d.quaternions.rotate_vector(offsets[jI], frame_rotations_world[parents[jI]]) + frame_positions_world[parents[jI]])
                
                    if len(children[jI]) > 0:
                        frame_rotations_world.append(t3d.quaternions.qmult(frame_rotations_world[parents[jI]], rot_local[fI, jI]))
                    else:
                        frame_rotations_world.append(t3d.quaternions.qeye())
                        
                #print("jI ", jI, " fpw ", frame_positions_world[jI].shape, " frw ", frame_rotations_world[jI].shape)
                
            frame_positions_world = np.stack(frame_positions_world, axis=0)
            frame_rotations_world = np.stack(frame_rotations_world, axis=0)
            
            #print("fI ", fI, " fpw ", frame_positions_world.shape, " frw ", frame_rotations_world.shape)
            
            positions_world.append(frame_positions_world)
            rotations_world.append(frame_rotations_world)
            
        positions_world = np.stack(positions_world, axis=0)
        rotations_world = np.stack(rotations_world, axis=0)
        
        return positions_world, rotations_world

    def euler_to_quat(self, rotations_euler, rot_sequence):
        
        rot_string = "".join([ "xyz"[i] for i in rot_sequence ])        
        seq_length = rotations_euler.shape[0]
        rotations_euler = np.reshape(rotations_euler, (-1, 3))
        rotations_quat = Rotation.from_euler(rot_string, rotations_euler, degrees=True).as_quat(scalar_first=True)
        
        rotations_quat = np.reshape(rotations_quat, (seq_length, -1, 4))
        
        return rotations_quat

    # for some unknown reason, euler_to_quat messes up when working with bvh data
    # this is a work around
    def euler_to_quat_bvh(self, rotations_euler, rot_sequence):
        
        # rotations_euler shape: F x J x D (F: frame count, J: joint count, D: 3 (with angles in degrees))
        # rot_sequence shape: D (D: 3 (integer indices))
        
        frame_count = rotations_euler.shape[0]
        joint_count = rotations_euler.shape[1]
        
        rotations_quat = []
        
        for fI in range(frame_count):
            
            joint_rotations_quat = []
            
            for jI in range(joint_count):
        
                # convert degrees to radians
                euler_x = rotations_euler[fI, jI, 0]/180.0 * math.pi;
                euler_y = rotations_euler[fI, jI, 1]/180.0 * math.pi;
                euler_z = rotations_euler[fI, jI, 2]/180.0 * math.pi;

                # convert euler rotation to quaternion
                quat_identity = t3d.quaternions.qeye()

                quat_x = t3d.quaternions.axangle2quat([1, 0, 0], euler_x)
                quat_y = t3d.quaternions.axangle2quat([0, 1, 0], euler_y)
                quat_z = t3d.quaternions.axangle2quat([0, 0, 1], euler_z)
                
                joint_rotation_quat = t3d.quaternions.qeye()
                
                rotations = [quat_x, quat_y, quat_z]
                for rot_index in rot_sequence:
                    joint_rotation_quat = t3d.quaternions.qmult(joint_rotation_quat, rotations[rot_index])
                
                joint_rotations_quat.append(joint_rotation_quat)
                
            joint_rotations_quat = np.stack(joint_rotations_quat, axis=0)
            
            rotations_quat.append(joint_rotations_quat)
                
        
        rotations_quat = np.stack(rotations_quat, axis=0)
        
        return rotations_quat
    def quat_to_euler(self, rotations_quat, rot_sequence):
        
        rot_string = "".join([ "xyz"[i] for i in rot_sequence ])
        seq_length = rotations_quat.shape[0]
        
        rotations_quat = np.reshape(rotations_quat, (-1, 4))
        rotations_euler = Rotation.from_quat(rotations_quat, scalar_first=True).as_euler(rot_string, degrees=True)
        rotations_euler = np.reshape(rotations_euler, (seq_length, -1, 3))
                
        return rotations_euler

    # for some unknown reason, quat_to_euler messes up when working with bvh data
    # this is a work around
    def quat_to_euler_bvh(self, rotations_quat, rot_sequence):
        
        # rotations_quat shape: F x J x D (F: frame count, J: joint count, D: 4 )
        # rot_sequence shape: D (D: 3 (integer indices))
        
        frame_count = rotations_quat.shape[0]
        joint_count = rotations_quat.shape[1]
        
        rotations_euler = []
        
        for fI in range(frame_count):
            
            joint_rotations_euler = []
            
            for jI in range(joint_count):
                
                rotation_quat = rotations_quat[fI, jI]
                #rotation_euler = np.array(t3d.euler.quat2euler(rotation_quat, axes="sxyz"))
                rotation_euler = np.array(t3d.euler.quat2euler(rotation_quat, axes="syxz"))
                rotation_euler  *= 180.0 / math.pi
                rotation_euler = np.array((rotation_euler[1], rotation_euler[0], rotation_euler[2]))

                joint_rotations_euler.append(rotation_euler)

            rotations_euler.append(joint_rotations_euler)
                
        rotations_euler = np.stack(rotations_euler, axis=0)
                
        return rotations_euler
    
    def remove_joints(self, mocap_data, joints_to_remove):
        """
        Remove the joints specified in 'joints_to_remove', both from the
        skeleton definition and from the dataset (which is modified in place).
        The rotations of removed joints are propagated along the kinematic chain.
        
        Important: assumes that the root joint is not removed
        """
        
        skeleton = mocap_data["skeleton"]
        
        parents = skeleton["parents"]
        children = skeleton["children"]
        joints = skeleton["joints"]

        # gather joints that are not removed
        valid_parents = []
        valid_children = []
        valid_joints = []
        
        for parent in range(len(parents)):
            if parent not in joints_to_remove:
                valid_parents.append(parent)
                valid_children.append(children[parent])
                valid_joints.append(joints[parent])
                
        #print("parents ", parents)
        #print("valid_parents ", valid_parents)
        #print("children ", children)
        #print("valid_children ", valid_children)
        #print("joints ", joints)
        #print("valid_joints ", valid_joints)
        
        # remove offsets / pos_local / rot_local_euler of non-valid joints
        motion = mocap_data["motion"]
        
        offsets = skeleton["offsets"]
        pos_local = motion["pos_local"]
        rot_local_euler = motion["rot_local_euler"]
        
        new_offsets = offsets[valid_parents, :]
        new_pos_local = pos_local[:, valid_parents, :]
        new_rot_local_euler = rot_local_euler[:, valid_parents, :]
        
        #print("offsets s ", offsets.shape)
        #print("new_offsets s ", new_offsets.shape)
        #print("pos_local s ", pos_local.shape)
        #print("new_pos_local s ", new_pos_local.shape)
        #print("rot_local_euler s ", rot_local_euler.shape)
        #print("new_rot_local_euler s ", new_rot_local_euler.shape)


                
        # renumber parents
        index_offsets = np.zeros(len(parents), dtype=int)
        
        new_parents = []
        for i, parent in enumerate(parents):
            if i not in joints_to_remove:
                new_parents.append(parent - index_offsets[parent])
            else:
                index_offsets[i:] += 1
                
        #print("valid_parents ", valid_parents)
        #print("new_parents ", new_parents)
                
        valid_to_new_parent_map = { valid_parents[i] : new_parents[i] for i in range(len(new_parents)) }
        
        #print("valid_to_new_parent_map ", valid_to_new_parent_map)
        
        # renumber children
        new_children = []
        for i, parent in enumerate(new_parents):
            new_children.append([])
        for i, parent in enumerate(new_parents):
            if parent != -1:
                new_children[parent].append(i)
        

        #print("valid_children ", valid_children)
        #print("new_children ", new_children)
        
        # new joint names
        new_joints = valid_joints
        
        # create new mocap data
        
        new_skeleton = {}
        new_skeleton["children"] = new_children
        new_skeleton["joints"] = new_joints
        new_skeleton["offsets"] = new_offsets
        new_skeleton["parents"] = new_parents
        new_skeleton["root"] = skeleton["root"]
        
        new_motion = {}
        new_motion["pos_local"] = new_pos_local
        new_motion["rot_local_euler"] = new_rot_local_euler
        
        new_mocap_data = {}
        
        new_mocap_data["frame_rate"] = mocap_data["frame_rate"]
        new_mocap_data["motion"] = new_motion
        new_mocap_data["rot_sequence"] = mocap_data["rot_sequence"]
        new_mocap_data["skeleton"] = new_skeleton
                
        return new_mocap_data
    
    def mocap_excerpt(self, mocap_data, start_frame=-1, end_frame=-1):
        
        mocap_data_excerpt = copy.deepcopy(mocap_data)
        
        motion_data = mocap_data_excerpt["motion"]

        full_frame_count = motion_data[list(motion_data.keys())[0]].shape[0]

        if start_frame == -1:
            start_frame = 0
    
        if end_frame == -1 or end_frame > full_frame_count:
            end_frame = full_frame_count
            
        for key in motion_data.keys():
            
            motion_data[key] = motion_data[key][start_frame:end_frame, ...]
            
            #values = values[start_frame:end_frame, ...]
            
        return mocap_data_excerpt

    # -------------------------------------------------------------------------------------------------
    # Utility: Math Helpers
    # -------------------------------------------------------------------------------------------------

    @staticmethod
    def interp_vectors(times_src, values_src, times_dst):
        out = np.zeros((len(times_dst),) + values_src.shape[1:], dtype=np.float32)
        flat_src = values_src.reshape(values_src.shape[0], -1)
        flat_out = out.reshape(len(times_dst), -1)
        for k in range(flat_src.shape[1]):
            flat_out[:, k] = np.interp(times_dst, times_src, flat_src[:, k])
        return out

    # -------------------------------------------------------------------------------------------------
    # Utility: Helpers for NPZ
    # -------------------------------------------------------------------------------------------------

    @staticmethod
    def build_children_from_parents(parents):
        children = [[] for _ in range(len(parents))]
        for j_idx, p_idx in enumerate(parents):
            if p_idx >= 0:
                children[p_idx].append(j_idx)
        return children

    @staticmethod
    def load_npz_topology(json_path):
        with open(json_path, "r") as f:
            topo = json.load(f)
        parents = topo["jointParents"]
        children = topo.get("jointChildren", Mocap_Tools.build_children_from_parents(parents))
        joints = topo.get("jointNames", [f"joint_{j}" for j in range(len(parents))])
        return parents, children, joints

    # -------------------------------------------------------------------------------------------------
    # Utility: Euler Timestamp Resampling for FBX/BVH
    # -------------------------------------------------------------------------------------------------

    @staticmethod
    def resample_euler_mocap_data(mocap_data, target_fps, time_ranges):
        times_dict = mocap_data["motion"].get("times", {})
        joints = mocap_data["skeleton"]["joints"]
        num_joints = len(joints)

        pos_local = mocap_data["motion"]["pos_local"]
        rot_local_euler = mocap_data["motion"]["rot_local_euler"]

        def get_joint_data(data, j_idx):
            if isinstance(data, list):
                return data[j_idx]
            else:
                return data[:, j_idx, :]

        joint_times_list = []
        for j_idx, j_name in enumerate(joints):
            if j_name in times_dict:
                j_times = times_dict[j_name]
            else:
                j_frames = len(get_joint_data(pos_local, j_idx))
                orig_fps = mocap_data.get("frame_rate", target_fps)
                j_times = np.arange(j_frames) / orig_fps
            joint_times_list.append(j_times)

        if time_ranges is None:
            max_t = 0.0
            for jt in joint_times_list:
                if len(jt) > 0:
                    max_t = max(max_t, jt[-1])
            time_ranges = [[0.0, max_t]]

        resampled_segments = []

        for t_range in time_ranges:
            start_time, end_time = t_range[0], t_range[1]
            target_times = np.arange(start_time, end_time, 1.0 / target_fps)
            num_frames = len(target_times)

            if num_frames == 0:
                continue

            new_pos_local = np.zeros((num_frames, num_joints, 3))
            new_rot_local_euler = np.zeros((num_frames, num_joints, 3))

            for j_idx in range(num_joints):
                j_times = joint_times_list[j_idx]
                j_pos = get_joint_data(pos_local, j_idx)
                j_rot = get_joint_data(rot_local_euler, j_idx)

                if len(j_times) == 0:
                    continue

                if len(j_times) == 1:
                    new_pos_local[:, j_idx, :] = j_pos[0]
                    new_rot_local_euler[:, j_idx, :] = j_rot[0]
                    continue

                for i in range(3):
                    new_pos_local[:, j_idx, i] = np.interp(target_times, j_times, j_pos[:, i])

                j_rot_rad = np.deg2rad(j_rot)
                j_rot_rad_unwrapped = np.unwrap(j_rot_rad, axis=0)
                j_rot_deg_unwrapped = np.rad2deg(j_rot_rad_unwrapped)

                for i in range(3):
                    new_rot_local_euler[:, j_idx, i] = np.interp(target_times, j_times, j_rot_deg_unwrapped[:, i])

            segment_data = copy.deepcopy(mocap_data)
            segment_data["motion"]["pos_local"] = new_pos_local
            segment_data["motion"]["rot_local_euler"] = new_rot_local_euler
            segment_data["frame_rate"] = target_fps

            if "times" in segment_data["motion"]:
                del segment_data["motion"]["times"]

            resampled_segments.append(segment_data)

        return resampled_segments

    # -------------------------------------------------------------------------------------------------
    # Utility: Timestamp Resampling for NPZ
    # -------------------------------------------------------------------------------------------------

    @staticmethod
    def resample_npz_mocap_data(np_data, target_fps, time_ranges, parents, children, joints=None):
        pos_local_times = np_data["/mocap/0/joint/pos_local_timestamps"]
        pos_local_values = np_data["/mocap/0/joint/pos_local_values"]
        rot_local_times = np_data["/mocap/0/joint/rot_local_timestamps"]
        rot_local_values = np_data["/mocap/0/joint/rot_local_values"]
        pos_world_times = np_data["/mocap/0/joint/pos_world_timestamps"]
        pos_world_values = np_data["/mocap/0/joint/pos_world_values"]
        rot_world_times = np_data["/mocap/0/joint/rot_world_timestamps"]
        rot_world_values = np_data["/mocap/0/joint/rot_world_values"]

        num_joints = pos_local_values.shape[1] // 3

        pos_local = pos_local_values.reshape((-1, num_joints, 3)).astype(np.float32)
        rot_local = rot_local_values.reshape((-1, num_joints, 4)).astype(np.float32)
        pos_world = pos_world_values.reshape((-1, num_joints, 3)).astype(np.float32)
        rot_world = rot_world_values.reshape((-1, num_joints, 4)).astype(np.float32)

        rot_local = rot_np.normalize(rot_np.fix_continuity(rot_local))
        rot_world = rot_np.normalize(rot_np.fix_continuity(rot_world))

        if joints is None:
            joints = [f"joint_{j}" for j in range(num_joints)]

        if time_ranges is None:
            start_time = float(max(pos_local_times[0], rot_local_times[0], pos_world_times[0], rot_world_times[0]))
            end_time = float(min(pos_local_times[-1], rot_local_times[-1], pos_world_times[-1], rot_world_times[-1]))
            time_ranges = [[start_time, end_time]]

        resampled_segments = []

        for start_time, end_time in time_ranges:
            target_times = np.arange(start_time, end_time, 1.0 / target_fps, dtype=np.float32)
            if len(target_times) < 2:
                continue

            new_pos_local = Mocap_Tools.interp_vectors(pos_local_times, pos_local, target_times)
            new_rot_local = Mocap_Tools.interp_vectors(rot_local_times, rot_local, target_times)
            new_pos_world = Mocap_Tools.interp_vectors(pos_world_times, pos_world, target_times)
            new_rot_world = Mocap_Tools.interp_vectors(rot_world_times, rot_world, target_times)

            new_rot_local = rot_np.normalize(new_rot_local)
            new_rot_world = rot_np.normalize(new_rot_world)

            offsets = np.zeros((num_joints, 3), dtype=np.float32)
            for j_idx in range(num_joints):
                parent_idx = parents[j_idx]
                if parent_idx == -1:
                    offsets[j_idx] = 0.0
                else:
                    bone_world = new_pos_world[0, j_idx] - new_pos_world[0, parent_idx]
                    parent_rot_world = new_rot_world[0, parent_idx:parent_idx + 1]
                    offsets[j_idx] = rot_np.rot(rot_np.conj(parent_rot_world), bone_world.reshape(1, 3))[0]

            segment_data = {
                "frame_rate": target_fps,
                "rot_sequence": [0, 1, 2],
                "skeleton": {
                    "offsets": offsets,
                    "parents": parents,
                    "children": children,
                    "joints": joints
                },
                "motion": {
                    "pos_local": new_pos_local,
                    "rot_local": new_rot_local
                }
            }
            resampled_segments.append(segment_data)

        return resampled_segments

    def _create_skeleton_data(self, bvh_data, mocap_data):
        
        skeleton_data = {}
        
        joint_names = [ joint_name for joint_name in bvh_data.skeleton.keys() ]
        joint_parent_names = [ joint_info["parent"] for joint_name, joint_info in bvh_data.skeleton.items() ]
        joint_parent_indices = [joint_names.index(joint_parent_name) if joint_parent_name in joint_names else -1 for joint_parent_name in joint_parent_names ]
        
        joint_child_indices = []
        for joint_info in bvh_data.skeleton.values():
            child_indices = []
            for child_name in joint_info["children"]:
                child_indices.append(joint_names.index(child_name))
            joint_child_indices.append(child_indices)
            
        joint_offsets = [ joint_info["offsets"] for joint_info in bvh_data.skeleton.values() ]
        joint_offsets = np.stack(joint_offsets, axis=0)
        
        skeleton_data["root"] = bvh_data.root_name
        skeleton_data["joints"] = joint_names
        skeleton_data["parents"] = joint_parent_indices
        skeleton_data["children"] = joint_child_indices
        skeleton_data["offsets"] = joint_offsets
        
        mocap_data["skeleton"] = skeleton_data
        
        return mocap_data
    
    def _create_motion_data(self, bvh_data, mocap_data):
        
        motion = {}
        
        joint_names = mocap_data["skeleton"]["joints"]
        joint_count = len(joint_names)
        rot_sequence = mocap_data["rot_sequence"]
        
        bvh_frames = bvh_data.values
        frame_count = bvh_frames.shape[0]
        
        bvh_frames_column_names = [ column for column in bvh_data.values.columns ]
        bvh_channels = set(bvh_data.channel_names)
        bvh_channel_joint_names = set([channel[0] for channel in bvh_channels])
        bvh_channel_value_names = ["Xposition", "Yposition", "Zposition", "Xrotation", "Yrotation", "Zrotation"]
        
        motion_translation = []
        motion_euler_rotation = []

        for joint_name in joint_names:

            if joint_name in bvh_channel_joint_names:
                joint_frames_combined = []      
            
                for i, value_name in enumerate(bvh_channel_value_names):
                    column_name = joint_name + "_" + value_name
                
                    if column_name in bvh_frames_column_names:
                        joint_frames_combined.append(np.array(bvh_frames[column_name]))
                    
                        #print("colname ", column_name, " values ", np.array(bvh_frames[column_name])[0])
                    
                    else:
                        joint_frames_combined.append(np.zeros(frame_count))

                joint_translations = joint_frames_combined[:3]
                joint_rotations = joint_frames_combined[3:]
                
                joint_translations = np.array(joint_translations)
                joint_rotations = np.array(joint_rotations)

                joint_translations = np.transpose(joint_translations)
                joint_rotations = np.transpose(joint_rotations)
                
                motion_translation.append(joint_translations)
                motion_euler_rotation.append(joint_rotations)
            else:
                
                joint_translations = np.zeros((frame_count, 3))
                joint_rotations = np.zeros((frame_count, 3))
                
                motion_translation.append(joint_translations)
                motion_euler_rotation.append(joint_rotations)
                
        motion_translation = np.stack(motion_translation, axis=1)
        motion_euler_rotation = np.stack(motion_euler_rotation, axis=1)
        
        motion["pos_local"] = motion_translation
        motion["rot_local_euler"] = motion_euler_rotation
        #motion["rot_local"] = self.euler_to_quat(motion_euler_rotation, rot_sequence)

        mocap_data["motion"] = motion
        
        return mocap_data

    def _create_bvh_channel_names(self, mocap_data):
        
        joints = mocap_data["skeleton"]["joints"]
        children = mocap_data["skeleton"]["children"]
        rot_sequence = mocap_data["rot_sequence"]
        
        pos_channel_names = ["Xposition", "Yposition", "Zposition"]
        rot_channel_names = ["Xrotation", "Yrotation", "Zrotation"]

        bvh_channels = pos_channel_names + [ rot_channel_names[i] for i in rot_sequence ]
        
        bvh_channel_names = []
        
        for jI, joint_name in enumerate(joints):
            
            # ignore nub joints
            if len(children[jI]) == 0:
                continue

            for channel_name in bvh_channels:
                
                bvh_channel_names.append((joint_name, channel_name))
            
        
        return bvh_channel_names, bvh_channels

    
    def _create_bvh_skeleton(self, mocap_data, bvh_channels):
        
        joints = mocap_data["skeleton"]["joints"]
        parents  = mocap_data["skeleton"]["parents"]
        children  = mocap_data["skeleton"]["children"]
        offsets = mocap_data["skeleton"]["offsets"]
        
        bvh_skeleton = {}
        
        for jI, joint_name in enumerate(joints):
            
            bvh_joint = {}
            
            # add name of parent joint
            if parents[jI] != -1:
                bvh_joint["parent"]  = joints[parents[jI]]
            else:
                bvh_joint["parent"] = None
            
            # add names of child joints
            bvh_joint["children"] = [ joints[child] for child in children[jI] ]
            
            # add joint offset
            bvh_joint["offsets"] = offsets[jI].tolist()
            
            # add joint channels
            if len(children[jI]) > 0:
                bvh_joint["channels"] = bvh_channels
            else: 
                bvh_joint["channels"] = []
            
                    
            bvh_skeleton[joint_name] = bvh_joint
            
        
        return bvh_skeleton
    
    def _create_bvh_frames(self, mocap_data, bvh_channels):
        
        joints = mocap_data["skeleton"]["joints"]
        children = mocap_data["skeleton"]["children"]
        pos_local = mocap_data["motion"]["pos_local"]
        rot_euler = mocap_data["motion"]["rot_local_euler"]
        rot_sequence = mocap_data["rot_sequence"]
        
        pos_channels = ["Xposition", "Yposition", "Zposition"]
        rot_channels = ["Xrotation", "Yrotation", "Zrotation"]
        
        bvh_frames = {}
        
        for jI, joint_name in enumerate(joints):
            
            if len(children[jI]) == 0:
                continue
            
            for channel_name in bvh_channels:
                
                col_name = joint_name + "_" + channel_name
                channel_values = []
                
                if channel_name in pos_channels:
                    channel_values = pos_local[:, jI, pos_channels.index(channel_name)].tolist()
                    
                elif channel_name in rot_channels:
                    channel_values = rot_euler[:, jI, rot_channels.index(channel_name)].tolist()

                else:
                    continue
                
                bvh_frames[col_name] = channel_values
                
        dataFrame = pandas.DataFrame(bvh_frames)
        
        return dataFrame