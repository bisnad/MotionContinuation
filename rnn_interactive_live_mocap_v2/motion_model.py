import torch
from torch import nn
from collections import OrderedDict
import math

"""
model architecture
"""

rnn_config = {
    "input_length": 64,
    "data_dim": 308,
    "node_dim": 512,
    "layer_count": 2,
    "device": "cuda",
    "weights_path": "results/weights/rnn_weights_epoch_400"
    }

vae_config = {
    "seq_length": 32,
    "data_dim": 308,
    "latent_dim": 8,
    "rnn_layer_count": 2,
    "rnn_layer_size": 512,
    "dense_layer_sizes": [512],
    "device": "cuda",
    "weights_path": ["results/weights/transformer_encoder_weights_epoch_200", "results/weights/transformer_decoder_weights_epoch_200"]
    }

class Reccurent(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim, layer_count):
        super(Reccurent, self).__init__()
        
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.layer_count = layer_count
        self.output_dim = output_dim
            
        rnn_layers = []
        
        rnn_layers.append(("rnn", nn.LSTM(self.input_dim, self.hidden_dim, self.layer_count, batch_first=True)))
        self.rnn_layers = nn.Sequential(OrderedDict(rnn_layers))
        
        dense_layers = []
        dense_layers.append(("dense", nn.Linear(self.hidden_dim, self.output_dim)))
        self.dense_layers = nn.Sequential(OrderedDict(dense_layers))
    
    def forward(self, x):
        x, (_, _) = self.rnn_layers(x)
        
        x = x[:, -1, :] # only last time step 
        x = self.dense_layers(x)
        
        return x
    
class Encoder(nn.Module):
    def __init__(self, sequence_length, data_dim, latent_dim, rnn_layer_count, rnn_layer_size, dense_layer_sizes):
        super(Encoder, self).__init__()
        
        self.sequence_length = sequence_length
        self.data_dim = data_dim
        self.latent_dim = latent_dim
        self.rnn_layer_count = rnn_layer_count
        self.rnn_layer_size = rnn_layer_size 
        self.dense_layer_sizes = dense_layer_sizes
    
        # create recurrent layers
        rnn_layers = []
        rnn_layers.append(("encoder_rnn_0", nn.LSTM(self.data_dim, self.rnn_layer_size, self.rnn_layer_count, batch_first=True)))
        
        self.rnn_layers = nn.Sequential(OrderedDict(rnn_layers))
        
        # create dense layers
        
        dense_layers = []
        
        dense_layers.append(("encoder_dense_0", nn.Linear(self.rnn_layer_size, self.dense_layer_sizes[0])))
        dense_layers.append(("encoder_dense_relu_0", nn.ReLU()))
        
        dense_layer_count = len(self.dense_layer_sizes)
        for layer_index in range(1, dense_layer_count):
            dense_layers.append(("encoder_dense_{}".format(layer_index), nn.Linear(self.dense_layer_sizes[layer_index-1], self.dense_layer_sizes[layer_index])))
            dense_layers.append(("encoder_dense_relu_{}".format(layer_index), nn.ReLU()))

        self.dense_layers = nn.Sequential(OrderedDict(dense_layers))
        
        # create final dense layers
            
        self.fc_mu = nn.Linear(self.dense_layer_sizes[-1], self.latent_dim)
        self.fc_std = nn.Linear(self.dense_layer_sizes[-1], self.latent_dim)
        
    def forward(self, x):

        x, (_, _) = self.rnn_layers(x)
        
        x = x[:, -1, :] # only last time step 
        x = self.dense_layers(x)
 
        mu = self.fc_mu(x)
        std = self.fc_std(x)

        return mu, std
    
class Decoder(nn.Module):
    def __init__(self, sequence_length, data_dim, latent_dim, rnn_layer_count, rnn_layer_size, dense_layer_sizes):
        super(Decoder, self).__init__()
        
        self.sequence_length = sequence_length
        self.data_dim = data_dim
        self.latent_dim = latent_dim
        self.rnn_layer_size = rnn_layer_size
        self.rnn_layer_count = rnn_layer_count
        self.dense_layer_sizes = dense_layer_sizes

        # create dense layers
        dense_layers = []
        
        dense_layers.append(("decoder_dense_0", nn.Linear(latent_dim, self.dense_layer_sizes[0])))
        dense_layers.append(("decoder_relu_0", nn.ReLU()))

        dense_layer_count = len(self.dense_layer_sizes)
        for layer_index in range(1, dense_layer_count):
            dense_layers.append(("decoder_dense_{}".format(layer_index), nn.Linear(self.dense_layer_sizes[layer_index-1], self.dense_layer_sizes[layer_index])))
            dense_layers.append(("decoder_dense_relu_{}".format(layer_index), nn.ReLU()))
 
        self.dense_layers = nn.Sequential(OrderedDict(dense_layers))
        
        # create rnn layers
        rnn_layers = []

        rnn_layers.append(("decoder_rnn_0", nn.LSTM(self.dense_layer_sizes[-1], self.rnn_layer_size, self.rnn_layer_count, batch_first=True)))
        
        self.rnn_layers = nn.Sequential(OrderedDict(rnn_layers))
        
        # final output dense layer
        final_layers = []
        
        final_layers.append(("decoder_dense_{}".format(dense_layer_count), nn.Linear(self.rnn_layer_size, self.data_dim)))
        
        self.final_layers = nn.Sequential(OrderedDict(final_layers))
        
    def forward(self, x):
        # dense layers
        x = self.dense_layers(x)
    
        # repeat vector
        x = torch.unsqueeze(x, dim=1)
        x = x.repeat(1, self.sequence_length, 1)
        
        # rnn layers
        x, (_, _) = self.rnn_layers(x)
        
        # final time distributed dense layer
        x_reshaped = x.contiguous().view(-1, self.rnn_layer_size)  # (batch_size * sequence, input_size)
        
        yhat = self.final_layers(x_reshaped)
        
        yhat = yhat.contiguous().view(-1, self.sequence_length, self.data_dim)

        return yhat
    
def createModels(rnn_config, vae_config):
    
    # create rnn
    rnn = Reccurent(rnn_config["data_dim"], rnn_config["node_dim"], rnn_config["data_dim"], rnn_config["layer_count"]).to( rnn_config["device"])

    if rnn_config["weights_path"] != "":
        if rnn_config["device"] == 'cuda':
            rnn.load_state_dict(torch.load(rnn_config["weights_path"]))
        else:
            rnn.load_state_dict(torch.load(rnn_config["weights_path"], map_location=torch.device(rnn_config["device"] )))

    rnn.eval()
    
    # create autoencoder
    encoder = Encoder(vae_config["seq_length"], vae_config["data_dim"], vae_config["latent_dim"], vae_config["rnn_layer_count"], vae_config["rnn_layer_size"], vae_config["dense_layer_sizes"]).to(vae_config["device"])
    
    ae_dense_layer_sizes_reversed = vae_config["dense_layer_sizes"].copy()
    ae_dense_layer_sizes_reversed.reverse()

    decoder = Decoder(vae_config["seq_length"], vae_config["data_dim"], vae_config["latent_dim"], vae_config["rnn_layer_count"], vae_config["rnn_layer_size"], ae_dense_layer_sizes_reversed).to(vae_config["device"])

    if len(vae_config["weights_path"]) > 0 and vae_config["weights_path"][0] != "":
        
        if vae_config["device"] == "cuda":
            encoder.load_state_dict(torch.load(vae_config["weights_path"][0]))
        else:
            encoder.load_state_dict(torch.load(vae_config["weights_path"][0], map_location=torch.device('cpu')))
    
    if len(vae_config["weights_path"]) > 1 and vae_config["weights_path"][1] != "":

        if vae_config["device"] == "cuda":        
            decoder.load_state_dict(torch.load(vae_config["weights_path"][1]))
        else:
            decoder.load_state_dict(torch.load(vae_config["weights_path"][1], map_location=torch.device('cpu')))

    encoder.eval()
    decoder.eval()
        
    return rnn, encoder, decoder
