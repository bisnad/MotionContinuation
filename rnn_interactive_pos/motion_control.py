import threading
import numpy as np
import transforms3d as t3d

from pythonosc import dispatcher
from pythonosc import osc_server


config = {"motion_seq": None,
          "synthesis": None,
          "gui": None,
          "input_length": 64,
          "ip": "127.0.0.1",
          "port": 9004}

class MotionControl():
    
    def __init__(self, config):
        
        self.motion_seq = config["motion_seq"]
        self.synthesis = config["synthesis"]
        self.gui = config["gui"]
        self.input_length = config["input_length"]
        self.ip = config["ip"]
        self.port = config["port"]
        
        self.dispatcher = dispatcher.Dispatcher()
        
        self.dispatcher.map("/mocap/seqindex", self.setSequenceIndex)
        self.dispatcher.map("/mocap/seqinput", self.setSequenceInput)
        self.dispatcher.map("/mocap/seqblend", self.setSequenceBlend)
        self.dispatcher.map("/mocap/setjointpos", self.setJointPosition)
        self.dispatcher.map("/mocap/changejointpos", self.changeJointPosition)
    
        self.server = osc_server.ThreadingOSCUDPServer((self.ip, self.port), self.dispatcher)
                
    def start_server(self):
        self.server.serve_forever()
        
    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def start(self):
        
        self.th = threading.Thread(target=self.start_server)
        self.th.start()
        
    def stop(self):
        
        self.th2 = threading.Thread(target=self.stop_server)
        self.th2.start()
        
    def setSequenceIndex(self, address, *args):
        
        seq_index = args[0]
        self.synthesis.setOrigSeqIndex(seq_index)
        
    def setSequenceInput(self, address, *args):
        
        if len(args) == 1: # start frame index
        
            seq_start_index = args[0]
            self.synthesis.setOrigSeqStartFrameIndex(seq_start_index)
            
        elif len(args) == 2: # frame index, frame count
        
            seq_start_index = args[0]
            seq_frame_count = args[1]
            
            self.synthesis.setOrigSeqStartFrameIndex(seq_start_index)
            self.synthesis.setOrigSeqFrameCount(seq_frame_count)
            
    def setSequenceBlend(self, address, *args):
        
        blend = args[0]
        
        self.synthesis.setOrigSeqBlend(blend)
        
    def setJointPosition(self, address, *args):
        
        joint_dim = self.synthesis.joint_dim
        
        if len(args) == 1 + joint_dim: # start frame index, position
        
            joint_index = args[0]
            joint_pos = np.array([args[1:1+joint_dim]])
            self.synthesis.setJointPosition(joint_index, joint_pos, 1)
            
        elif len(args) > 1 + joint_dim:
            
            joint_pos = np.array([args[-joint_dim:-1]])
            
            for aI in range(len(args) - 4):
                joint_index = args[aI]
                
                self.synthesis.setJointPosition(joint_index, joint_pos, 1)
  
            
    def changeJointPosition(self, address, *args):
        
        joint_dim = self.synthesis.joint_dim
        
        if len(args) ==  1 + joint_dim: # start frame index, position
        
            joint_index = args[0]
            joint_pos = np.array([args[1:1+joint_dim]])
            self.synthesis.changeJointPosition(joint_index, joint_pos, 1)
            
        elif len(args) > 1 + joint_dim:
            
            joint_pos = np.array([args[-joint_dim:-1]])

            for aI in range(len(args) - 4):
                joint_index = args[aI]
                
                self.synthesis.changeJointPosition(joint_index, joint_pos, 1)
