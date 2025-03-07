# AI-Toolbox - Motion Continuation

The "Motion Continuation" category of the AI-Toolbox contains a collection of python-based generative machine learning models that can be trained on motion capture data. Once trained, the models then take as input as short motion excerpt and output synthetic motions that represent a continuation of the excerpt. The synthetic motions represent variations of the original motion capture data and preserve its aesthetic and stylistic properties. 

The following tools are available:

- [rnn](rnn)

  A Python-based tool for training a motion-continuation model on motion capture data. 

- [rnn_interactive](rnn_interactive)

  A Python-based tool that employ a previously trained motion continuation model to generate synthetic motions in real-time. Here, the short motion excerpt that serves as initial input to the model stems from a motion capture recording. 

- [rnn_interactive_live_mocap](rnn_interactive_live_mocap)

  A Python-based tool that employs a previously trained motion continuation model to generate synthetic motions in real-time. Here, the short motion excerpt that serves as initial input to the model is live captured.


- [rnn_interactive_pos](rnn_interactive_pos)

  A Python-based tool that employs a previously trained motion continuation model to generate synthetic motions in real-time. Contrary to the other tools, this model works with skeleton joint positions that have been obtained by using one of the 2D or 3D pose estimation tools provided by the AI-Toolbox (see MotionAnalysys/PoseEstimation). 
