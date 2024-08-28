import torch
import numpy as np

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from pathlib import Path

from threading import Thread, Event
import time
from time import sleep
import datetime

import motion_synthesis

config = {"synthesis": None,
          "sender": None,
          "update_interval": 0.02,
          "view_min": np.array([-100, -100, -100], dtype=np.float32),
          "view_max": np.array([100, 100, 100], dtype=np.float32),
          "view_ele": 90,
          "view_azi": -90,
          "view_dist": 250,
          "view_line_width": 2.0
    }

class MotionGui(QtWidgets.QWidget):
    
    def __init__(self, config):
        super().__init__()
        
        self.synthesis = config["synthesis"]
        self.sender = config["sender"]
        
        self.edges = self.synthesis.edge_list
        
        self.pose_thread_interval = config["update_interval"]
        
        self.view_min = config["view_min"]
        self.view_max = config["view_max"]
        self.view_ele = config["view_ele"]
        self.view_azi = config["view_azi"]
        self.view_dist = config["view_dist"]
        self.view_line_width = config["view_line_width"]
        
        # dynamic canvas
        self.pose_canvas = gl.GLViewWidget()
        self.pose_canvas_lines = gl.GLLinePlotItem()
        self.pose_canvas_points = gl.GLScatterPlotItem()
        self.pose_canvas.addItem(self.pose_canvas_lines)
        self.pose_canvas.addItem(self.pose_canvas_points)
        self.pose_canvas.setCameraParams(distance=self.view_dist)
        self.pose_canvas.setCameraParams(azimuth=self.view_azi)
        self.pose_canvas.setCameraParams(elevation=self.view_ele)

        self.q_start_buttom = QtWidgets.QPushButton("start", self)
        self.q_start_buttom.clicked.connect(self.start)  
        
        self.q_stop_buttom = QtWidgets.QPushButton("stop", self)
        self.q_stop_buttom.clicked.connect(self.stop)  
        
        self.q_button_grid = QtWidgets.QGridLayout()
        self.q_button_grid.addWidget(self.q_start_buttom,0,0)
        self.q_button_grid.addWidget(self.q_stop_buttom,0,1)

        self.q_grid = QtWidgets.QGridLayout()
        self.q_grid.addWidget(self.pose_canvas,0,0)
        self.q_grid.addLayout(self.q_button_grid,1,0)
        
        self.q_grid.setRowStretch(0, 0)
        self.q_grid.setRowStretch(1, 0)
        
        self.setLayout(self.q_grid)

        self.setGeometry(50,50,512,612)
        self.setWindowTitle("Sequence Continuation")
        
    def start(self):
        self.pose_thread_event = Event()
        self.pose_thread = Thread(target = self.update)
        
        self.pose_thread.start()
        
    def stop(self):
        self.pose_thread_event.set()
        self.pose_thread.join()
                
    def update(self):
        
        while self.pose_thread_event.is_set() == False:

            start_time = time.time()            

            self.update_pred_seq()
            self.update_seq_plot()
            self.update_osc()
            
            end_time = time.time()   
            
            #print("update time ", end_time - start_time, " interval ", self.pose_thread_interval)
            
            next_update_interval = max(self.pose_thread_interval - (end_time - start_time), 0.0)
            
            #sleep(self.pose_thread_interval)
            sleep(next_update_interval)

            
    def update_pred_seq(self):
        
        self.synthesis.update()       
        self.synth_pose_wpos = self.synthesis.synth_pose_wpos
        
    def update_osc(self):
        
        self.sender.send("/mocap/joint/pos_world", self.synth_pose_wpos)

    def update_seq_plot(self):
        
        pose = self.synth_pose_wpos
        joint_count = self.synthesis.joint_count
        joint_dim = self.synthesis.joint_dim
        
        if joint_dim == 2: # add third axis to pose sequence
            pose3D = np.concatenate([pose, np.zeros((1, joint_count, 1))], axis=2)[0]
        else:
            pose3D = pose[0]

        lines_data = pose3D[np.array(self.edges).flatten()]
        
        self.pose_canvas_lines.setData(pos=lines_data, mode="lines", color=(1.0, 1.0, 1.0, 0.5), width=self.view_line_width)
        #self.pose_canvas_lines.setData(pos=lines_data, mode="lines", color=(0.0, 0.0, 0.0, 1.0), width=self.view_line_width)
        self.pose_canvas_points.setData(pos=pose3D, color=(1.0, 1.0, 1.0, 1.0))

        #self.pose_canvas.show()
        
        #print(self.pose_canvas.cameraParams())