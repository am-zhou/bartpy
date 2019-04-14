import numpy as np

from bartpy.model import Model
from bartpy.node import LeafNode
from bartpy.samplers.sampler import Sampler
from bartpy.samplers.scalar import NormalScalarSampler


class LeafNodeSampler(Sampler):
    """
    Responsible for generating samples of the leaf node predictions
    Essentially just draws from a normal distribution with prior specified by model parameters

    Uses a cache of draws from a normal(0, 1) distribution to improve sampling performance
    """

    def __init__(self,
                 scalar_sampler=NormalScalarSampler(50000)):
        self._scalar_sampler = scalar_sampler

    def step(self, model: Model, node: LeafNode) -> float:
        sampled_value = self.sample(model, node)
        node.set_value(sampled_value)
        return sampled_value

    def sample(self, model: Model, node: LeafNode) -> float:
        prior_var = model.sigma_m ** 2
        n = node.data.n_obsv
        likihood_var = (model.sigma.current_value() ** 2) / n
        likihood_mean = np.mean(node.data.y)
        posterior_variance = 1. / (1. / prior_var + 1. / likihood_var)
        posterior_mean = likihood_mean * (prior_var / (likihood_var + prior_var))
        return posterior_mean + (self._scalar_sampler.sample() * np.power(posterior_variance / model.n_trees, 0.5))
