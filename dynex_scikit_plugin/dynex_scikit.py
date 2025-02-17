from __future__ import annotations

import itertools
import logging
import tempfile
import typing
import warnings

import dimod
import numpy as np
import numpy.typing as npt
import dimod
import dynex

from sklearn.base import BaseEstimator
from sklearn.feature_selection import SelectorMixin
from sklearn.utils.validation import check_is_fitted

from utilities import corrcoef #utilities.py

__all__ = ["SelectFromQuadraticModel"]


class SelectFromQuadraticModel(SelectorMixin, BaseEstimator):
    """Select features using a quadratic optimization problem solved on a hybrid solver.

    Args:
        alpha:
            Hyperparameter between 0 and 1 that controls the relative weight of
            the relevance and redundancy terms.
            ``alpha=0`` places no weight on the quality of the features,
            therefore the features will be selected as to minimize the
            redundancy without any consideration to quality.
            ``alpha=1`` places the maximum weight on the quality of the features,
            and therefore will be equivalent to using
            :class:`sklearn.feature_selection.SelectKBest`.
        num_features:
            The number of features to select.
        time_limit:
            The time limit for the run on the hybrid solver.

    """

    ACCEPTED_METHODS = [
        "correlation",
        # "mutual information",  # todo
        ]

    def __init__(
        self,
        *,
        alpha: float = .5,
        method: str = "correlation",  # undocumented until there is another supported
        num_features: int = 10,
        time_limit: typing.Optional[float] = None,
    ):
        if not 0 <= alpha <= 1:
            raise ValueError(f"alpha must be between 0 and 1, given {alpha}")

        if method not in self.ACCEPTED_METHODS:
            raise ValueError(
                f"method must be one of {self.ACCEPTED_METHODS}, given {method}"
            )

        if num_features <= 0:
            raise ValueError(f"num_features must be a positive integer, given {num_features}")

        self.alpha = alpha
        self.method = method
        self.num_features = num_features
        self.time_limit = time_limit  # check this lazily

    def __sklearn_is_fitted__(self) -> bool:
        # used by `check_is_fitted()`
        try:
            self._mask
        except AttributeError:
            return False

        return True

    def _get_support_mask(self) -> np.ndarray[typing.Any, np.dtype[np.bool_]]:
        """Get the boolean mask indicating which features are selected

        Returns:
          boolean array of shape [# input features]. An element is True iff its
          corresponding feature is selected for retention.

        Raises:
            RuntimeError: This method will raise an error if it is run before `fit`
        """
        #disabled check_is_fitted(self)

        try:
            return self._mask
        except AttributeError:
            raise RuntimeError("fit hasn't been run yet")

    @staticmethod
    def correlation_cqm(
        X: npt.ArrayLike,
        y: npt.ArrayLike,
        *,
        alpha: float,
        num_features: int,
        strict: bool = True,
    ) -> dimod.ConstrainedQuadraticModel:
        """Build a constrained quadratic model for feature selection.

        This method is based on maximizing influence and independence as
        measured by correlation [Milne et al.]_.

        Args:
            X:
                Feature vectors formatted as a numerical 2D array-like.
            y:
                Class labels formatted as a numerical 1D array-like.
            alpha:
                Hyperparameter between 0 and 1 that controls the relative weight of
                the relevance and redundancy terms.
                ``alpha=0`` places no weight on the quality of the features,
                therefore the features will be selected as to minimize the
                redundancy without any consideration to quality.
                ``alpha=1`` places the maximum weight on the quality of the features,
                and therefore will be equivalent to using
                :class:`sklearn.feature_selection.SelectKBest`.
            num_features:
                The number of features to select.
            strict:
                If ``False`` the constraint on the number of selected features
                is ``<=`` rather than ``==``.

        Returns:
            A constrained quadratic model.

        .. [Milne et al.] Milne, Andrew, Maxwell Rounds, and Phil Goddard. 2017. "Optimal Feature
            Selection in Credit Scoring and Classification Using a Quantum Annealer."
            1QBit; White Paper.
            https://1qbit.com/whitepaper/optimal-feature-selection-in-credit-scoring-classification-using-quantum-annealer
        """

        X = np.atleast_2d(np.asarray(X))
        y = np.asarray(y)

        if X.ndim != 2:
            raise ValueError("X must be a 2-dimensional array-like")

        if y.ndim != 1:
            raise ValueError("y must be a 1-dimensional array-like")

        if y.shape[0] != X.shape[0]:
            raise ValueError(f"requires: X.shape[0] == y.shape[0] but {X.shape[0]} != {y.shape[0]}")

        if not 0 <= alpha <= 1:
            raise ValueError(f"alpha must be between 0 and 1, given {alpha}")

        if num_features <= 0:
            raise ValueError(f"num_features must be a positive integer, given {num_features}")

        if X.shape[0] <= 1:
            raise ValueError("X must have at least two rows")

        cqm = dimod.ConstrainedQuadraticModel()
        cqm.add_variables(dimod.BINARY, X.shape[1])

        # add the k-hot constraint
        cqm.add_constraint(
            ((v, 1) for v in cqm.variables),
            '==' if strict else '<=',
            num_features,
            label=f"{num_features}-hot",
            )

        with tempfile.TemporaryFile() as fX, tempfile.TemporaryFile() as fout:
            # we make a copy of X because we'll be modifying it in-place within
            # some of the functions
            X_copy = np.memmap(fX, X.dtype, mode="w+", shape=(X.shape[0], X.shape[1] + 1))
            X_copy[:, :-1] = X
            X_copy[:, -1] = y

            # make the matrix that will hold the correlations
            correlations = np.memmap(
                fout,
                dtype=np.result_type(X, y),
                mode="w+",
                shape=(X_copy.shape[1], X_copy.shape[1]),
                )

            # main calculation. It modifies X_copy in-place
            corrcoef(X_copy, out=correlations, rowvar=False, copy=False)

            # we don't care about the direction of correlation in terms of
            # the penalty/quality
            np.absolute(correlations, out=correlations)

            # our objective
            # we multiply by 2 because the matrix is symmetric
            np.fill_diagonal(correlations, correlations[:, -1] * (-2 * alpha * num_features))

            # Note: the full symmetric matrix (with both upper- and lower-diagonal
            # entries for each correlation coefficient) is retained for consistency with
            # the original formulation from Milne et al.
            it = np.nditer(correlations[:-1, :-1], flags=['multi_index'], op_flags=[['readonly']])
            cqm.set_objective((*it.multi_index, x) for x in it if x)

        return cqm

    def fit(
        self,
        X: npt.ArrayLike,
        y: npt.ArrayLike,
        *,
        alpha: typing.Optional[float] = None,
        num_features: typing.Optional[int] = None,
        time_limit: typing.Optional[float] = None,
    ) -> SelectFromQuadraticModel:
        """Select the features to keep.

        Args:
            X:
                Feature vectors formatted as a numerical 2D array-like.
            y:
                Class labels formatted as a numerical 1D array-like.
            alpha:
                Hyperparameter between 0 and 1 that controls the relative weight of
                the relevance and redundancy terms.
                ``alpha=0`` places no weight on the quality of the features,
                therefore the features will be selected as to minimize the
                redundancy without any consideration to quality.
                ``alpha=1`` places the maximum weight on the quality of the features,
                and therefore will be equivalent to using
                :class:`sklearn.feature_selection.SelectKBest`.
            num_features:
                The number of features to select.
                Defaults to the value provided to the constructor.
            time_limit:
                The time limit for the run on the hybrid solver.
                Defaults to the value provided to the constructor.

        Returns:
            This instance of `SelectFromQuadraticModel`.
        """
        X = np.atleast_2d(np.asarray(X))
        if X.ndim != 2:
            raise ValueError("X must be a 2-dimensional array-like")

        # y is checked by the correlation method function

        if alpha is None:
            alpha = self.alpha
        # alpha is checked by the correlation method function

        if num_features is None:
            num_features = self.num_features
        # num_features is checked by the correlation method function

        # time_limit is checked by the LeapHybridCQMSampelr

        # if we already have fewer features than requested, just return
        if num_features >= X.shape[1]:
            self._mask = np.ones(X.shape[1], dtype=bool)
            return self

        if self.method == "correlation":
            cqm = self.correlation_cqm(X, y, num_features=num_features, alpha=alpha)
        else:
            raise ValueError(f"only methods {self.acceptable_methods} are implemented")

        # run on Dynex platform:
        bqm, invert = dimod.cqm_to_bqm(cqm) # Convert CQM->BQM
        model = dynex.BQM(bqm);
        sampler = dynex.DynexSampler(model);
        sampleset_x = sampler.sample(num_reads=2048, annealing_time = 200, mainnet=False, debugging=False);
        sampleset = invert(sampler.assignments);
        lowest = sampler.dimod_assignments.first.sample;
        feasible = cqm.check_feasible(sampleset, atol=0.1);
        print('Best energy:', sampler.dimod_assignments.first.energy,"feasible?", feasible);


        self._mask = np.fromiter((lowest[v] for v in cqm.variables),
                                 count=cqm.num_variables(), dtype=bool)

        return self

    def unfit(self):
        """Undo a previously executed ``fit`` method."""
        del self._mask
