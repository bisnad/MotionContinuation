## AI-Toolbox - Motion Continuation - RNN

![MocapPlayer](./data/media/MocapPlayer.JPG)

Figure 1. Screenshot of the Motion Continuation tool after training concluded. The window on the left shows the console output of the learning progress. The window depicts the learning progress as graph plot. 

### Summary

This Python-based tool implements a machine learning model that can be trained to generate synthetic motion data that represent a continuation of an short motion excerpt.  This tool doesn't operate in real-time and is used exclusively for training the machine learning model. Once a model is trained, it can be used in combination with other tools that generate synthetic motions in real-time and can be interactively controlled. 

### Installation

The software runs within the *premiere* anaconda environment. For this reason, this environment has to be setup beforehand.  Instructions how to setup the *premiere* environment are available as part of the [installation documentation ](https://github.com/bisnad/AIToolbox/tree/main/Installers) in the [AI Toolbox github repository](https://github.com/bisnad/AIToolbox). 

The software can be downloaded by cloning the [MotionContinuation Github repository](https://github.com/bisnad/MotionContinuation). After cloning, the software is located in the MotionContinuation / rnn directory.

### Directory Structure

- rnn
  - common (contains python scripts for handling mocap data)
  - data 
    - configs (contains configurations for skeleton representations used by various Pose Estimation systems and lists of loss weights for joints in skeletons that include hand joints)
    - media (contains media used in this Readme)
    - mocap (contains an example mocap recording)
  - results
    - anims (after training, contains synthetic motion data exported as Gif animations and FBX/BVH files)
    - histories (after training, contains logs of the training process as csv file and graph plot)
    - weights (after training, contains the weights of the trained model)

### Usage

#### Start

The tool exists in two versions. The version named `rnn` is used to train the machine learning model on motion data that stores both joint rotations and positions and that is stored in BVH or FBX format. The version named `rnn_pos` is used to train the machine learning model on motion data that stores only joint positions and that is stored in PKL format. The first version of the tool can be started by double clicking the rnn.bat (Windows) or rnn.sh (MacOS) shell scripts or by typing the following commands into the Anaconda terminal:

```
conda activate premiere
cd MocapContinuation/rnn
python rnn.py
```

The second version of the tool can be started by double clicking the rnn_pos.bat (Windows) or rnn_pos.sh (MacOS) shell scripts or by typing the following commands into the Anaconda terminal:

```
conda activate premiere
cd MocapContinuation/rnn
python rnn_pos.py
```

#### Functionality

##### RNN Motion Data Import

The tool imports motion data from one or several motion files. These motion files are then used to create the training set. By default, the tool loads the motion file in the `data/mocap` folder. To read different motion files, the the following source code in the file rnn.py has to be modified:

```
mocap_file_path = "data/mocap/"
mocap_files = ["Muriel_Embodied_Machine_variation.fbx"]
mocap_valid_frame_ranges = [ [ [ 200, 6400 ] ] ]
mocap_pos_scale = 1.0
mocap_fps = 50
mocap_loss_weights_file = None
```

The string value assigned to the variable `mocap_file_path` specifies the path to the folder that contains motion data files. The list of string values assigned to the variable `mocap_files` specifies the names of the motion data files that will be loaded. The nested list of integer values that is assigned to the variable `mocap_valid_frame_ranges` specifies for each motion data file the frame ranges that should be used for training. Each frame range is defined by a start and end frame. It is possible to specify multiple frame ranges per motion data file. Any frames outside of these frame ranges will be excluded from training. The float value assigned to the variable `mocap_pos_scale` specifies a scaling value that is applied to joint positions. The purpose of the scaling value is to bring the position values to cm units. The integer value assigned to the variable `mocap_fps` specifies the number of frames per second with which the motion data is stored. This value has no influence on training but affects the synthetic motion data that is exported at the end of a training run. The string value assigned to the variable `mocap_loss_weights_file` specifies the path to a configuration file that contains loss scales for skeleton joints. Loading such a configuration file is recommended for motion  data that contains multiple joints per hand in order to reduce the influence of the hand joints on the overall loss calculation during training. If the motion data doesn't contain multiple hand joints, then this variable can be set to `None`. 

##### RNN_Pos Motion Data Import

TODO: Also create a proper pose estimation recording of a muriel video.



To specify a different mocap file during software startup, the following source code in the file mocap_player.py has to be modified:





#### Default Mocap File

When the software starts, it automatically reads an example motion capture file that is located in the MocapPlayer/data/mocap folder. An alternative mocap file can be read either when the software starts or while it is running. In the latter case, the software can only read mocap files that contain the same skeleton topology as the mocap file that was read during startup. To read a different mocap file during software startup, the following source code in the file mocap_player.py has to be modified:

```
motion_player.config = { 
    "file_name": "data/mocap/Muriel_Take1.fbx",
    "fps": 50
    }
```

In this code, the string "data/mocap/Muriel_Take1.fbx" needs to be replaced to specify a path to a different mocap file. If necessary, the number "50" can also be replaced to specify a different frame rate for playback.  

#### Functionality

The software can play motion capture recordings of a single performer that have been saved in [FBX](https://en.wikipedia.org/wiki/FBX#:~:text=FBX%20(from%20Filmbox)%20is%20a,series%20of%20video%20game%20middleware.) or [BVH](https://en.wikipedia.org/wiki/Biovision_Hierarchy#:~:text=BioVision%20Hierarchy%20(BVH)%20is%20a,acquired%20by%20Motion%20Analysis%20Corporation.)) format. The playback loops between a user specified start and end frame. While a recording is played, the recorded performer is graphically depicted as a simple stick figure. Also, while the software plays, it sends for each frame the joint information of the performer as [OSC](https://en.wikipedia.org/wiki/Open_Sound_Control) data. This information includes the joint position and rotation both in global and local coordinates. In global coordinates, joint positions and rotations are relative to an absolute reference position and rotation in space.  In local coordinates, joint positions and rotations are relative to the positions and rotations of the parent joints.  Before closing the software, playback has to be stopped.

#### Graphical User Interface

The graphical user interface of the software provides the following functionality or conveys the following information (from top to bottom and left to right):

- A text to display the currently loaded mocap file.
- A text to display the start end frame of the playback range and the currently played frame. 
- A graphical window to display the pose corresponding to the current motion capture frame as a simple stick figure. The rotation, position, and scale of the stick figure can be changed with the mouse. Dragging with the left mouse button rotates the figure. Dragging with the middle mouse button moves the figure. Operating the scroll wheel zooms the figure in and out. 
- A slider to display and change the currently played frame.  
- A slider to set the start frame of the playback range.
- A slider to set the end frame of the playback range.
- Three buttons to load, start, and stop a mocap recording. 
- A number box to change the frames per seconds with which the mocap recording is played.
- An toggle to turn OSC sending on and off.
- Four number boxes to change the IP address to which OSC data is sent to.
- A number box to change the port number box to which OSC data is sent to. 

### OSC Communication

The software sends the following OSC messages representing the joint positions and rotations of the currently displayed motion capture figure.
Each message contains all the joint positions and rotations grouped together. In the OSC messages described below, N represents the number of joints.

The following OSC messages are sent by the software:

- joint positions as list of 3D vectors relative to parent joint: `/mocap/0/joint/pos_local <float j1x> <float j1y> <float j1z> .... <float jNx> <float jNy> <float jNz>` 
- joint positions as list of 3D vectors in world coordinates: `/mocap/0/joint/pos_world <float j1x> <float j1y> <float j1z> .... <float jNx> <float jNy> <float jNz>` 
- joint rotations as list of Quaternions relative to parent joint: `/mocap/0/joint/rot_local <float j1w> <float j1x> <float j1y> <float j1z> .... <float jNw> <float jNx> <float jNy> <float jNz>` 
- joint rotations as list of Quaternions in world coordinates: `/mocap/0/joint/rot_local <float j1w> <float j1x> <float j1y> <float j1z> .... <float jNw> <float jNx> <float jNy> <float jNz>` 

The software can be remote controlled by sending OSC messages to it. By default, the software receives OSC messages on port 9002. To change this port, the following source code in the file mocap_player.py has to be modified:

```
motion_control.config["port"] = 9002
```

In this code, the number 9002 needs to be replaced to specify a different port.

An example Max patch demonstrates the use of the remote control functionality. The following OSC messages can be used to remote control the software. 

- load a motion capture file: `/player/load <string filename>`
- start playback: `/player/start`
- stop playback: `/player/stop`
- set playback speed in fps (frames per second): `/player/fps <int fps>`
- set playback position to specific frame: `/player/frame <int frame>`
- set start frame of playback loop: `/player/start_frame <int frame>`
- set end frame of playback loop: `/player/end_frame <int frame>`

### Limitations and Bugs

- The player only supports motion capture recordings that contain a single person.

- It reads only motion capture recordings in FBX format in which each skeleton pose has its own keyframe and in which the number of keyframes is the same for all skeleton joints.
- The software hangs if it is closed while playing. Always stop the playback before closing the software!



