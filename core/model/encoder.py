# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/Encoder.ipynb (unless otherwise specified).

__all__ = ['Encoder']

# Cell

from ..scene_graph.scene_graph import SceneGraph
from torchvision.models import resnet34
import torch.nn as nn
import torch

# Cell

class Encoder(nn.Module):
    def __init__(self, dim = 256):
        super().__init__()

        """
        Input:
            dim : final number of dimensions of the node and spatial embeddings

        Returns:
            Intialises a model which has node embeddimgs and spatial embeddings
        """


        self.dim = dim
        self.resnet = resnet34(pretrained=True)
        self.feature_extractor = nn.Sequential(*list(self.resnet.children())[:-3])

        self.scene_graph = SceneGraph(feature_dim=self.dim,
                                 output_dims=[self.dim,self.dim],
                                 downsample_rate=16)


    def forward(self,feed_dict):
        """
        Input:
            feed_dict: a dictionary containing list tensors containing images and bounding box data.
            Each element of the feed_dict corresponds to one elment of the batch.
            Inside each batch are contained ["image": Image tensor,
                                             "boxes":Bounding box tensor,
                                             bounding box
                                            ]
        """
        num_batch = feed_dict["images"].shape[0]
        num_total_nodes = feed_dict["objects"].sum().item()

        image_features = self.feature_extractor(feed_dict["images"])
        outputs = self.scene_graph(image_features, feed_dict["objects_boxes"], feed_dict["objects"])

        node_features = outputs[0][0]
        for num in range(1,num_batch):
            node_features = torch.cat([node_features, outputs[num][0]], dim =0)

        # To be implemented
        spatial_features = None

        return outputs, node_features, spatial_features