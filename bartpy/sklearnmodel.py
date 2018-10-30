from copy import deepcopy
from typing import List, Callable, Tuple, Mapping

import numpy as np
import pandas as pd
from joblib import Parallel, delayed
from sklearn.base import RegressorMixin, BaseEstimator

from bartpy.data import Data
from bartpy.model import Model
from bartpy.samplers.leafnode import LeafNodeSampler
from bartpy.samplers.modelsampler import ModelSampler
from bartpy.samplers.schedule import SampleSchedule
from bartpy.samplers.sigma import SigmaSampler
from bartpy.samplers.treemutation.treemutation import TreeMutationSampler
from bartpy.samplers.treemutation.uniform.likihoodratio import UniformTreeMutationLikihoodRatio
from bartpy.samplers.treemutation.uniform.proposer import UniformMutationProposer
from bartpy.sigma import Sigma

ChainExtract = Tuple[List['Model'], np.ndarray]
Extract = List[ChainExtract]


def run_chain(model: 'SklearnModel', X: np.ndarray, y: np.ndarray):
    """
    Run a single chain for a model
    Primarily used as a building block for constructing a parallel run of multiple chains
    """
    model.model = model._construct_model(X, y)
    return model.sampler.samples(model.model,
                                 model.n_samples,
                                 model.n_burn,
                                 model.thin,
                                 model.store_in_sample_predictions,
                                 model.store_acceptance_trace)


def delayed_run_chain():
    return run_chain


class SklearnModel(BaseEstimator, RegressorMixin):
    """
    The main access point to building BART models in BartPy

    Parameters
    ----------
    n_trees: int
        the number of trees to use, more trees will make a smoother fit, but slow training and fitting
    n_chains: int
        the number of independent chains to run
        more chains will improve the quality of the samples, but will require more computation
    sigma_a: float
        shape parameter of the prior on sigma
    sigma_b: float
        scale parameter of the prior on sigma
    n_samples: int
        how many recorded samples to take
    n_burn: int
        how many samples to run without recording to reach convergence
    thin: float
        percentage of samples to store.
        use this to save memory when running large models
    p_grow: float
        probability of choosing a grow mutation in tree mutation sampling
    p_prune: float
        probability of choosing a prune mutation in tree mutation sampling
    alpha: float
        prior parameter on tree structure
    beta: float
        prior parameter on tree structure
    store_in_sample_predictions: bool
        whether to store full prediction samples
        set to False if you don't need in sample results - saves a lot of memory
    store_acceptance_trace: bool
        whether to store acceptance rates of the gibbs samples
        unless you're very memory constrained, you wouldn't want to set this to false
        useful for diagnostics
    n_jobs: int
        how many cores to use when computing MCMC samples
        set to `-1` to use all cores
    """

    def __init__(self,
                 n_trees: int = 50,
                 n_chains: int = 4,
                 sigma_a: float = 0.001,
                 sigma_b: float = 0.001,
                 n_samples: int = 200,
                 n_burn: int = 200,
                 thin: float = 0.1,
                 p_grow: float = 0.5,
                 p_prune: float = 0.5,
                 alpha: float = 0.95,
                 beta: float = 2.,
                 store_in_sample_predictions: bool=True,
                 store_acceptance_trace: bool=True,
                 n_jobs=-1):
        self.n_trees = n_trees
        self.n_chains = n_chains
        self.sigma_a = sigma_a
        self.sigma_b = sigma_b
        self.n_burn = n_burn
        self.n_samples = n_samples
        self.p_grow = p_grow
        self.p_prune = p_prune
        self.alpha = alpha
        self.beta = beta
        self.thin = thin
        self.n_jobs = n_jobs
        self.store_in_sample_predictions = store_in_sample_predictions
        self.store_acceptance_trace = store_acceptance_trace
        self.columns = None

        self.proposer = UniformMutationProposer([self.p_grow, self.p_prune])
        self.likihood_ratio = UniformTreeMutationLikihoodRatio([self.p_grow, self.p_prune])
        self.tree_sampler = TreeMutationSampler(self.proposer, self.likihood_ratio)
        self.schedule = SampleSchedule(self.tree_sampler, LeafNodeSampler(), SigmaSampler())
        self.sampler = ModelSampler(self.schedule)

        self.sigma, self.data, self.model, self._prediction_samples, self._model_samples, self.extract = [None] * 6

    def fit(self, X: np.ndarray, y: np.ndarray) -> 'SklearnModel':
        """
        Learn the model based on training data

        Parameters
        ----------
        X: pd.DataFrame
            training covariates
        y: np.ndarray
            training targets

        Returns
        -------
        SklearnModel
            self with trained parameter values
        """
        self.model = self._construct_model(X, y)
        self.extract = Parallel(n_jobs=self.n_jobs)(self.f_delayed_chains(X, y))
        self.combined_chains = self._combine_chains(self.extract)
        self._model_samples, self._prediction_samples = self.combined_chains["model"], self.combined_chains["in_sample_predictions"]
        self._acceptance_trace = self.combined_chains["acceptance"]
        return self

    @staticmethod
    def _combine_chains(extract):
        keys = list(extract[0].keys())
        combined = {}
        for key in keys:
            combined[key] = np.concatenate([chain[key] for chain in extract], axis=0)
        return combined

    @staticmethod
    def _convert_covariates_to_data(X: np.ndarray, y: np.ndarray) -> Data:
        from copy import deepcopy
        if type(X) == pd.DataFrame:
            X: pd.DataFrame = X
            X = X.values
        return Data(deepcopy(X), deepcopy(y), normalize=True)

    def _construct_model(self, X: np.ndarray, y: np.ndarray) -> Model:
        self.data = self._convert_covariates_to_data(X, y)
        self.sigma = Sigma(self.sigma_a, self.sigma_b, self.data.normalizing_scale)
        self.model = Model(self.data, self.sigma, n_trees=self.n_trees, alpha=self.alpha, beta=self.beta)
        return self.model

    def f_delayed_chains(self, X: np.ndarray, y: np.ndarray):
        """
        Access point for getting access to delayed methods for running chains
        Useful for when you want to run multiple instances of the model in parallel
        e.g. when calculating a null distribution for feature importance

        Parameters
        ----------
        X: np.ndarray
            Covariate matrix
        y: np.ndarray
            Target array

        Returns
        -------
        List[Callable[None, ChainExtract]]
        """
        return [delayed(x)(self, X, y) for x in self.f_chains()]

    def f_chains(self) -> List[Callable[None, Extract]]:
        """
        List of methods to run MCMC chains
        Useful for running multiple models in parallel

        Returns
        -------
        List[Callable[None, Extract]]
            List of method to run individual chains
            Length of n_chains
        """
        return [delayed_run_chain() for _ in range(self.n_chains)]

    def predict(self, X: np.ndarray=None) -> np.ndarray:
        """
        Predict the target corresponding to the provided covariate matrix
        If X is None, will predict based on training covariates

        Prediction is based on the mean of all samples

        Parameters
        ----------
        X: pd.DataFrame
            covariates to predict from

        Returns
        -------
        np.ndarray
            predictions for the X covariates
        """
        if X is None and self.store_in_sample_predictions:
            return self.data.unnormalize_y(self._prediction_samples.mean(axis=0))
        elif X is None and not self.store_in_sample_predictions:
            raise ValueError(
                "In sample predictions only possible if model.store_in_sample_predictions is `True`.  Either set the parameter to True or pass a non-None X parameter")
        else:
            return self._out_of_sample_predict(X)

    def residuals(self, X=None, y=None) -> np.ndarray:
        """
        Array of error for each observation

        Parameters
        ----------
        X: np.ndarray
            Covariate matrix
        y: np.ndarray
            Target array

        Returns
        -------
        np.ndarray
            Error for each observation
        """
        if y is None:
            return self.model.data.unnormalized_y - self.predict(X)
        else:
            return y - self.predict(X)

    def l2_error(self, X=None, y=None) -> np.ndarray:
        """
        Calculate the squared errors for each row in the covariate matrix

        Parameters
        ----------
        X: np.ndarray
            Covariate matrix
        y: np.ndarray
            Target array
        Returns
        -------
        np.ndarray
            Squared error for each observation
        """
        return np.square(self.residuals(X, y))

    def rmse(self, X, y) -> float:
        """
        The total RMSE error of the model
        The sum of squared errors over all observations

        Parameters
        ----------
        X: np.ndarray
            Covariate matrix
        y: np.ndarray
            Target array

        Returns
        -------
        float
            The total summed L2 error for the model
        """
        return np.sqrt(np.sum(self.l2_error(X, y)))

    def _out_of_sample_predict(self, X):
        return self.data.unnormalize_y(np.mean([x.predict(X) for x in self._model_samples], axis=0))

    def fit_predict(self, X, y):
        self.fit(X, y)
        if self.store_in_sample_predictions:
            return self.predict()
        else:
            return self.predict(X)

    @property
    def model_samples(self) -> List[Model]:
        """
        Array of the model as it was after each sample.
        Useful for examining for:

         - examining the state of trees, nodes and sigma throughout the sampling
         - out of sample prediction

        Returns None if the model hasn't been fit

        Returns
        -------
        List[Model]
        """
        return self._model_samples

    @property
    def acceptance_trace(self) -> List[Mapping[str, float]]:
        """
        List of Mappings from variable name to acceptance rates

        Each entry is the acceptance rate of the variable in each iteration of the model

        Returns
        -------
        List[Mapping[str, float]]
        """
        return self._acceptance_trace

    @property
    def prediction_samples(self) -> np.ndarray:
        """
        Matrix of prediction samples at each point in sampling
        Useful for assessing convergence, calculating point estimates etc.

        Returns
        -------
        np.ndarray
            prediction samples with dimensionality n_samples * n_points
        """
        return self.prediction_samples

    def from_extract(self, extract: Extract, X: np.ndarray, y: np.ndarray) -> 'SklearnModel':
        """
        Create a copy of the model using an extract
        Useful for doing operations on extracts created in external processes like feature selection
        Parameters
        ----------
        extract: Extract
            samples produced by delayed chain methods
        X: np.ndarray
            Covariate matrix
        y: np.ndarray
            Target variable

        Returns
        -------
        SklearnModel
            Copy of the current model with samples
        """
        new_model = deepcopy(self)
        self._model_samples, self._prediction_samples = self.combined_chains["model"], self.combined_chains["in_sample_predictions"]
        self._acceptance_trace = self.combined_chains["acceptance"]
        new_model.data = self._convert_covariates_to_data(X, y)
        return new_model
