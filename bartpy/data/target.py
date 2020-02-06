import numpy as np


class Target(object):
    """

    Encapsualtes the target we're trying to predict at a particular node in the decision tree

    Provides a clean interface for pulling information about the target e.g.
      - the total summed target

    Additionally, provides aggressive caching to help performance
    """
    def __init__(self, y, mask, n_obsv, normalize, y_sum=None):

        if normalize:
            self.original_y_min, self.original_y_max = y.min(), y.max()
            self._y = self.normalize_y(y)
        else:
            self._y = y

        self._mask = mask
        self._inverse_mask_int = (~self._mask).astype(int)
        self._n_obsv = n_obsv

        if y_sum is None:
            self.y_sum_cache_up_to_date = False
            self._summed_y = None
        else:
            self.y_sum_cache_up_to_date = True
            self._summed_y = y_sum

    @staticmethod
    def normalize_y(y: np.ndarray) -> np.ndarray:
        """
        Normalize y into the range (-0.5, 0.5)
        Useful for allowing the leaf parameter prior to be 0, and to standardize the sigma prior

        Parameters
        ----------
        y - np.ndarray

        Returns
        -------
        np.ndarray

        Examples
        --------
        >>> Target.normalize_y([1, 2, 3])
        array([-0.5,  0. ,  0.5])
        """
        y_min, y_max = np.min(y), np.max(y)
        return -0.5 + ((y - y_min) / (y_max - y_min))

    def unnormalize_y(self, y: np.ndarray) -> np.ndarray:
        distance_from_min = y - (-0.5)
        total_distance = (self.original_y_max - self.original_y_min)
        return self.original_y_min + (distance_from_min * total_distance)

    @property
    def unnormalized_y(self) -> np.ndarray:
        return self.unnormalize_y(self.values)

    @property
    def normalizing_scale(self) -> float:
        return self.original_y_max - self.original_y_min

    def summed_y(self) -> float:
        if self.y_sum_cache_up_to_date:
            return self._summed_y
        else:
            self._summed_y = np.sum(self._y * self._inverse_mask_int)
            self.y_sum_cache_up_to_date = True
            return self._summed_y

    def update_y(self, y) -> None:
        self._y = y
        self.y_sum_cache_up_to_date = False

    @property
    def values(self):
        return self._y


if __name__ == "__main__":
    import doctest
    doctest.testmod()