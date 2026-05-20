import torch
import numpy as np
import datetime
import os

from PyQt5 import QtWidgets, QtCore
from PyQt5.QtCore import Qt
import pyqtgraph as pg
import pyqtgraph.Vector as qVector
import pyqtgraph.opengl as gl
from pathlib import Path

from threading import Thread, Event
import time
from time import sleep

import motion_synthesis
from common import fbx_tools as fbx  # Added for FBX exporting

config = {"synthesis": None,
          "sender": None,
          "update_interval": 0.02,
          "view_min": np.array([-100, -100, -100], dtype=np.float32),
          "view_max": np.array([100, 100, 100], dtype=np.float32),
          "view_center": np.array([0, 0, 100], dtype=np.float32),
          "view_ele": 90,
          "view_azi": -90,
          "view_dist": 250,
          "view_line_width": 2.0,
          "osc_ip": "127.0.0.1",
          "osc_port": 9007
    }

class PoseCanvasUpdater(QtCore.QObject):
    request_canvas_update = QtCore.pyqtSignal()

class CustomGLViewWidget(gl.GLViewWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opts['rotationMethod'] = 'quaternion'

    def mouseMoveEvent(self, ev):
        lpos = ev.position() if hasattr(ev, 'position') else ev.localPos()

        if not hasattr(self, 'mousePos'):
            self.mousePos = lpos

        diff = lpos - self.mousePos
        self.mousePos = lpos

        if ev.buttons() == QtCore.Qt.LeftButton:
            if ev.modifiers() & QtCore.Qt.ControlModifier:
                self.pan(diff.x(), diff.y(), 0, relative='view')
            else:
                self.orbit(-diff.x(), diff.y())
        elif ev.buttons() == QtCore.Qt.MiddleButton:
            if ev.modifiers() & QtCore.Qt.ControlModifier:
                self.pan(diff.x(), 0, diff.y(), relative='view-upright')
            else:
                self.pan(diff.x(), diff.y(), 0, relative='view-upright')
        else:
            super().mouseMoveEvent(ev)


class MotionGui(QtWidgets.QWidget):
    
    def __init__(self, config):
        super().__init__()
        
        self.synthesis = config["synthesis"]
        self.sender = config["sender"]
        
        self.edges = self.synthesis.edge_list
        self.pose_thread_interval = config["update_interval"]
        
        # Recording State
        self.is_recording = False
        self.record_buffer_pos = []
        self.record_buffer_rot = []
        
        self.view_min = config["view_min"]
        self.view_max = config["view_max"]
        self.view_ele = config["view_ele"]
        self.view_azi = config["view_azi"]
        self.view_dist = config["view_dist"]
        self.view_line_width = config["view_line_width"]
        
        view_center = config.get("view_center", np.array([0, 0, 100], dtype=np.float32))
        self.view_center = qVector(view_center[0], view_center[1], view_center[2])
        
        self.pose_canvas = CustomGLViewWidget()
        self.pose_canvas_lines = gl.GLLinePlotItem()
        self.pose_canvas_points = gl.GLScatterPlotItem()
        self.pose_canvas.addItem(self.pose_canvas_lines)
        self.pose_canvas.addItem(self.pose_canvas_points)
        
        self.pose_canvas.setCameraParams(center=self.view_center)
        self.pose_canvas.setCameraParams(distance=self.view_dist)
        self.pose_canvas.setCameraParams(azimuth=self.view_azi)
        self.pose_canvas.setCameraParams(elevation=self.view_ele)

        # Buttons
        self.q_start_buttom = QtWidgets.QPushButton("Start", self)
        self.q_start_buttom.clicked.connect(self.start)  
        
        self.q_stop_buttom = QtWidgets.QPushButton("Stop", self)
        self.q_stop_buttom.clicked.connect(self.stop)  

        self.q_record_button = QtWidgets.QPushButton("Record", self)
        self.q_record_button.setCheckable(True)
        self.q_record_button.clicked.connect(self.toggle_recording)

        self.q_exit_button = QtWidgets.QPushButton("Exit", self)
        self.q_exit_button.clicked.connect(self.exit_application)
        
        self.q_button_grid = QtWidgets.QHBoxLayout()
        self.q_button_grid.addWidget(self.q_start_buttom)
        self.q_button_grid.addWidget(self.q_stop_buttom)
        self.q_button_grid.addWidget(self.q_record_button)
        self.q_button_grid.addWidget(self.q_exit_button)

        # -----------------------------
        # GUI ELEMENTS (Controls on Right Side)
        # -----------------------------
        self.q_controls_layout = QtWidgets.QFormLayout()
        expanding_policy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        
        # OSC IP and Port Controls
        self.q_osc_ip = QtWidgets.QLineEdit(config.get("osc_ip", "127.0.0.1"))
        self.q_osc_ip.setSizePolicy(expanding_policy)
        self.q_osc_ip.textChanged.connect(self.change_osc_ip)
        self.q_controls_layout.addRow("OSC Send IP:", self.q_osc_ip)
        
        self.q_osc_port = QtWidgets.QSpinBox()
        self.q_osc_port.setRange(1024, 65535)
        self.q_osc_port.setValue(config.get("osc_port", 9004))
        self.q_osc_port.setSizePolicy(expanding_policy)
        self.q_osc_port.valueChanged.connect(self.change_osc_port)
        self.q_controls_layout.addRow("OSC Send Port:", self.q_osc_port)

        # Sequence Index
        self.q_seq_index = QtWidgets.QSpinBox()
        self.q_seq_index.setMinimum(0)
        self.q_seq_index.setMaximum(max(0, len(self.synthesis.orig_sequences) - 1))
        self.q_seq_index.setValue(self.synthesis.orig_seq_index)
        self.q_seq_index.setSizePolicy(expanding_policy)
        self.q_seq_index.valueChanged.connect(self.change_seq_index)
        self.q_controls_layout.addRow("Seq Index:", self.q_seq_index)
        
        # Start Time
        self.q_start_time = QtWidgets.QDoubleSpinBox()
        self.q_start_time.setMinimum(0.0)
        self.q_start_time.setDecimals(3)
        self.q_start_time.setSingleStep(0.1)
        self.q_start_time.setSizePolicy(expanding_policy)
        self.update_start_time_bounds()
        current_time = self.synthesis.orig_seq_start_frame_index / max(1, self.synthesis.mocap_fps)
        self.q_start_time.setValue(current_time)
        self.q_start_time.valueChanged.connect(self.change_start_time)
        self.q_controls_layout.addRow("Start Time (s):", self.q_start_time)
        
        # PI Temperature
        self.q_pi_temp = QtWidgets.QDoubleSpinBox()
        self.q_pi_temp.setRange(0.1, 2.5)
        self.q_pi_temp.setSingleStep(0.1)
        self.q_pi_temp.setValue(self.synthesis.pi_temperature)
        self.q_pi_temp.setSizePolicy(expanding_policy)
        self.q_pi_temp.valueChanged.connect(lambda v: self.synthesis.setPiTemp(v))
        self.q_controls_layout.addRow("MDN PI Temp:", self.q_pi_temp)
        
        # Gaussian Temperature
        self.q_gauss_temp = QtWidgets.QDoubleSpinBox()
        self.q_gauss_temp.setRange(0.0, 2.0)
        self.q_gauss_temp.setSingleStep(0.1)
        self.q_gauss_temp.setValue(self.synthesis.gaussian_temp)
        self.q_gauss_temp.setSizePolicy(expanding_policy)
        self.q_gauss_temp.valueChanged.connect(lambda v: self.synthesis.setGaussianTemp(v))
        self.q_controls_layout.addRow("MDN Gauss Temp:", self.q_gauss_temp)
        
        # Top P
        self.q_top_p = QtWidgets.QDoubleSpinBox()
        self.q_top_p.setRange(0.5, 0.95)
        self.q_top_p.setSingleStep(0.05)
        self.q_top_p.setValue(self.synthesis.top_p)
        self.q_top_p.setSizePolicy(expanding_policy)
        self.q_top_p.valueChanged.connect(lambda v: self.synthesis.setTopP(v))
        self.q_controls_layout.addRow("MDN TopP:", self.q_top_p)
        
        # 1 Euro Cutoff
        self.q_cutoff = QtWidgets.QDoubleSpinBox()
        self.q_cutoff.setRange(0.0, 10.0)
        self.q_cutoff.setSingleStep(0.1)
        self.q_cutoff.setValue(self.synthesis.one_euro_filter.mincutoff)
        self.q_cutoff.setSizePolicy(expanding_policy)
        self.q_cutoff.valueChanged.connect(lambda v: self.synthesis.one_euro_filter.setMinCutOff(v))
        self.q_controls_layout.addRow("1€ Cutoff:", self.q_cutoff)
        
        # 1 Euro Beta
        self.q_beta = QtWidgets.QDoubleSpinBox()
        self.q_beta.setRange(0.0, 1.0)
        self.q_beta.setSingleStep(0.01)
        self.q_beta.setValue(self.synthesis.one_euro_filter.beta)
        self.q_beta.setSizePolicy(expanding_policy)
        self.q_beta.valueChanged.connect(lambda v: self.synthesis.one_euro_filter.setBeta(v))
        self.q_controls_layout.addRow("1€ Beta:", self.q_beta)

        scroll_widget = QtWidgets.QWidget()
        scroll_widget.setLayout(self.q_controls_layout)
        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidget(scroll_widget)
        scroll_area.setWidgetResizable(True)
        scroll_area.setMinimumWidth(320)

        self.q_grid = QtWidgets.QGridLayout()
        self.q_grid.addWidget(self.pose_canvas, 0, 0)
        self.q_grid.addWidget(scroll_area, 0, 1, 2, 1)  
        self.q_grid.addLayout(self.q_button_grid, 1, 0)
        
        self.q_grid.setRowStretch(0, 1)
        self.q_grid.setColumnStretch(0, 1)
        self.q_grid.setColumnStretch(1, 0)
        
        self.setLayout(self.q_grid)
        self.setGeometry(50, 50, 900, 700)
        self.setWindowTitle("Motion Continuation")

        self.poseCanvasUpdater = PoseCanvasUpdater()
        self.poseCanvasUpdater.request_canvas_update.connect(self.update_pose_plot)

    def change_osc_ip(self, text):
        self.sender.config["ip"] = text
        if hasattr(self.sender, 'client'):
            try:
                from pythonosc import udp_client
                self.sender.client = udp_client.SimpleUDPClient(text, self.q_osc_port.value())
            except ImportError:
                pass

    def change_osc_port(self, val):
        self.sender.config["port"] = val
        if hasattr(self.sender, 'client'):
            try:
                from pythonosc import udp_client
                self.sender.client = udp_client.SimpleUDPClient(self.q_osc_ip.text(), val)
            except ImportError:
                pass

    def toggle_recording(self):
        self.is_recording = self.q_record_button.isChecked()
        if self.is_recording:
            self.q_record_button.setText("Stop Recording")
            self.record_buffer_pos = []
            self.record_buffer_rot = []
            print("Recording started...")
        else:
            self.q_record_button.setText("Record")
            self.save_recording()

    def save_recording(self):
        if len(self.record_buffer_rot) == 0:
            print("No frames were recorded.")
            return
            
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recorded_motion_{timestamp}.fbx"
        
        # 1. Convert Quaternions to Euler Angles (degrees)
        rot_local_quat = np.array(self.record_buffer_rot)
        rot_local_euler = np.zeros((*rot_local_quat.shape[:-1], 3))
        
        # Attempt conversion using available toolkit or fallback to SciPy
        try:
            from common.mocap_tools import Mocap_Tools
            mocap_t = Mocap_Tools()
            # If your mocap_tools has a quat_to_euler function
            if hasattr(mocap_t, 'quat_to_euler'):
                rot_local_euler = mocap_t.quat_to_euler(rot_local_quat, [0,1,2])
            else:
                from scipy.spatial.transform import Rotation
                # SciPy fallback (assuming standard xyzw quaternion format)
                # Reshape to a flat list of quaternions, convert, then reshape back
                flat_quats = rot_local_quat.reshape(-1, 4)
                # Note: if your quaternions are wxyz, you might need to roll the axis first: np.roll(flat_quats, -1, axis=-1)
                flat_eulers = Rotation.from_quat(flat_quats).as_euler('xyz', degrees=True)
                rot_local_euler = flat_eulers.reshape((*rot_local_quat.shape[:-1], 3))
        except Exception as e:
            print(f"Warning: Could not properly convert quaternions to euler angles. FBX rotations may be broken. ({e})")
        
        # 2. Build the FBX_Mocap_Data object
        from common.fbx_tools import FBX_Mocap_Data, FBX_Tools
        
        fbx_data = FBX_Mocap_Data()
        skel = self.synthesis.skeleton
        
        # Fill skeleton definition
        fbx_data.skeleton_joints = skel.get("joints", [])
        fbx_data.skeleton_children = skel.get("children", [])
        fbx_data.skeleton_parents = skel.get("parents", [])
        fbx_data.skeleton_joint_offsets = skel.get("offsets", [])
        fbx_data.skeleton_root_node = None 
        fbx_data.skeleton_nodes = []
        
        # Fill motion sequences
        fbx_data.motion_rot_sequence = [0, 1, 2] # Standard XYZ Euler rotation sequence
        fbx_data.motion_frame_rate = float(self.synthesis.mocap_fps)
        fbx_data.motion_frame_count = len(self.record_buffer_pos)
        fbx_data.motion_pos_local = np.array(self.record_buffer_pos)
        fbx_data.motion_rot_local_euler = rot_local_euler
        fbx_data.system_unit = "cm" 

        # 3. Export using write(mocap_data_list, filename)
        exporter = FBX_Tools()
        try:
            # fbx_tools expects a list of mocap_data objects (one per skeleton in the scene)
            exporter.write([fbx_data], filename)
            print(f"Successfully saved recording to: {filename}")
        except Exception as e:
            print(f"Error saving FBX file: {e}")

    def update_start_time_bounds(self):
        fps = max(1, self.synthesis.mocap_fps)
        seq_length = self.synthesis.seq_length
        total_frames = self.synthesis.orig_sequences[self.synthesis.orig_seq_index].shape[0]
        
        max_start_frame = max(0, total_frames - seq_length)
        max_time_seconds = max_start_frame / fps
        self.q_start_time.setMaximum(max_time_seconds)
        
    def change_seq_index(self, val):
        self.synthesis.setOrigSeqIndex(val)
        self.update_start_time_bounds()
        
    def change_start_time(self, val):
        fps = max(1, self.synthesis.mocap_fps)
        frame_index = int(val * fps)
        self.synthesis.setOrigSeqStartFrameIndex(frame_index)
        
    def sync_gui_with_synthesis(self):
        def update_val(widget, val, tolerance=1e-4):
            if abs(widget.value() - val) > tolerance:
                widget.blockSignals(True)
                widget.setValue(val)
                widget.blockSignals(False)

        update_val(self.q_seq_index, self.synthesis.orig_seq_index)
        self.update_start_time_bounds()
        current_time = self.synthesis.orig_seq_start_frame_index / max(1, self.synthesis.mocap_fps)
        update_val(self.q_start_time, current_time)
        update_val(self.q_pi_temp, self.synthesis.pi_temperature)
        update_val(self.q_gauss_temp, self.synthesis.gaussian_temp)
        update_val(self.q_top_p, self.synthesis.top_p)
        update_val(self.q_cutoff, self.synthesis.one_euro_filter.mincutoff)
        update_val(self.q_beta, self.synthesis.one_euro_filter.beta)
        
    def start(self):
        self.pose_thread_event = Event()
        self.pose_thread = Thread(target = self.update)
        self.pose_thread.start()
        
    def stop(self):
        self.pose_thread_event.set()
        self.pose_thread.join()

    def exit_application(self):
        """Cleanly stops the thread and closes the application."""
        # Stop the prediction thread if it is currently running
        if hasattr(self, 'pose_thread_event') and not self.pose_thread_event.is_set():
            self.stop()
            
        # Close the PyQt window (triggers app.lastWindowClosed in the main script)
        self.close()
                
    def update(self):
        while self.pose_thread_event.is_set() == False:
            start_time = time.time()            
            self.update_pred_seq()
            self.poseCanvasUpdater.request_canvas_update.emit()  
            self.update_osc()
            end_time = time.time()   
            next_update_interval = max(self.pose_thread_interval - (end_time - start_time), 0.0)
            sleep(next_update_interval)

    def update_pred_seq(self):
        self.synthesis.update()       
        self.synth_pose_wpos = self.synthesis.synth_pose_wpos
        self.synth_pose_wrot = self.synthesis.synth_pose_wrot
        self.synth_pose_lrot = self.synthesis.synth_pose_lrot
        
    def update_osc(self):
        self.synth_pose_wpos_rh = np.copy(self.synth_pose_wpos)
        self.synth_pose_wrot_rh = np.copy(self.synth_pose_wrot)
        self.synth_pose_lrot_rh = np.copy(self.synth_pose_lrot)
        
        self.sender.send("/mocap/0/joint/pos_world", self.synth_pose_wpos_rh)
        self.sender.send("/mocap/0/joint/rot_world", self.synth_pose_wrot_rh)
        self.sender.send("/mocap/0/joint/rot_local", self.synth_pose_lrot_rh)

        # Buffer frames when recording
        if self.is_recording:
            # Reconstruct pos_local shape (Num_Joints, 3). Root has pos, children have zero offset.
            pos_local = np.zeros_like(self.synth_pose_wpos)
            pos_local[0] = self.synth_pose_wpos[0] 
            
            self.record_buffer_pos.append(pos_local)
            self.record_buffer_rot.append(np.copy(self.synth_pose_lrot))

    def update_pose_plot(self):
        self.sync_gui_with_synthesis()
        pose = self.synth_pose_wpos
        points_data = pose
        lines_data = pose[np.array(self.edges).flatten()]
        
        self.pose_canvas_lines.setData(pos=lines_data, mode="lines", color=(1.0, 1.0, 1.0, 0.5), width=self.view_line_width)
        self.pose_canvas_points.setData(pos=pose, color=(1.0, 1.0, 1.0, 0.5))