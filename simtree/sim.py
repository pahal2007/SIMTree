import numpy as np
from copy import deepcopy
from matplotlib import gridspec
import matplotlib.pyplot as plt

from sklearn.linear_model import Lasso
from sklearn.utils.extmath import softmax
from sklearn.preprocessing import LabelBinarizer
from sklearn.utils import check_X_y, column_or_1d
from sklearn.model_selection import train_test_split
from sklearn.utils.validation import check_is_fitted
from sklearn.base import BaseEstimator, RegressorMixin, ClassifierMixin, is_classifier, is_regressor

from abc import ABCMeta, abstractmethod
from .smspline import SMSplineRegressor, SMSplineClassifier


__all__ = ["SimRegressor", "SimClassifier"]


class BaseSim(BaseEstimator, metaclass=ABCMeta):

    @abstractmethod
    def __init__(self, reg_lambda=0, reg_gamma=1e-5, knot_num=5, degree=3, random_state=0):

        self.reg_lambda = reg_lambda
        self.reg_gamma = reg_gamma
        self.knot_num = knot_num
        self.degree = degree
        self.random_state = random_state

    def _first_order_thres(self, x, y):

        """calculate the projection indice using the first order stein's identity subject to hard thresholding

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        y : array-like of shape (n_samples,)
            containing target values
        Returns
        -------
        np.array of shape (n_features, 1)
            the normalized projection inidce
        """

        if self.reg_lambda == 0:
            mu = np.average(x, axis=0)
            cov = np.cov(x.T)
            inv_cov = np.linalg.pinv(cov, 1e-7)
            s1 = np.dot(inv_cov, (x - mu).T).T
            zbar = np.average(y.reshape(-1, 1) * s1, axis=0)
        else:
            mx = x.mean(0)
            sx = x.std(0) + 1e-7
            nx = (x - mx) / sx
            lr = Lasso(alpha=self.reg_lambda)
            lr.fit(nx, y)
            zbar = lr.coef_ / sx
        if np.linalg.norm(zbar) > 0:
            beta = zbar / np.linalg.norm(zbar)
        else:
            beta = zbar
        return beta.reshape([-1, 1])

    def fit(self, x, y):

        """fit the Sim model

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        y : array-like of shape (n_samples,)
            containing target values
        Returns
        -------
        object
            self : Estimator instance.
        """

        np.random.seed(self.random_state)
        x, y = self._validate_input(x, y)
        n_samples, n_features = x.shape
        self.beta_ = self._first_order_thres(x, y)

        if len(self.beta_[np.abs(self.beta_) > 0]) > 0:
            if (self.beta_[np.argmax(np.abs(self.beta_))] < 0):
                self.beta_ = - self.beta_
        xb = np.dot(x, self.beta_)
        self._estimate_shape(xb, y, np.min(xb), np.max(xb))
        return self

    def decision_function(self, x):

        """output f(beta^T x) for given samples

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        Returns
        -------
        np.array of shape (n_samples,)
            containing f(beta^T x)
        """

        check_is_fitted(self, "beta_")
        check_is_fitted(self, "shape_fit_")
        xb = np.dot(x, self.beta_)
        pred = self.shape_fit_.decision_function(xb)
        return pred

    def fit_middle_update_adam(self, x, y, val_ratio=0.2, tol=0.0001,
                  max_middle_iter=100, n_middle_iter_no_change=5, max_inner_iter=100, n_inner_iter_no_change=5,
                  batch_size=100, learning_rate=1e-3, beta_1=0.9, beta_2=0.999, stratify=True, verbose=False):

        """fine tune the fitted Sim model using middle update method (adam)

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        y : array-like of shape (n_samples,)
            containing target values
        val_ratio : float, optional, default=0.2
            the split ratio for validation set
        tol : float, optional, default=0.0001
            the tolerance for early stopping
        max_middle_iter : int, optional, default=3
            the maximal number of middle iteration
        n_middle_iter_no_change : int, optional, default=3
            the tolerance of non-improving middle iterations
        max_inner_iter : int, optional, default=100
            the maximal number of inner iteration (epoch) for "adam" optimizer
        n_inner_iter_no_change : int, optional, default=5
            the tolerance of non-improving inner iteration for adam optimizer
        batch_size : int, optional, default=100
            the batch_size for adam optimizer
        learning_rate : float, optional, default=1e-4
            the learning rate for adam optimizer
        beta_1 : float, optional, default=0.9
            the beta_1 parameter for adam optimizer
        beta_2 : float, optional, default=0.999
            the beta_1 parameter for adam optimizer
        stratify : bool, optional, default=True
            whether to stratify the target variable when splitting the validation set
        verbose : bool, optional, default=False
            whether to show the training history
        """
            
        x, y = self._validate_input(x, y)
        n_samples = x.shape[0]
        if is_regressor(self):
            idx1, idx2 = train_test_split(np.arange(n_samples), test_size=val_ratio,
                                          random_state=self.random_state)
            tr_x, tr_y, val_x, val_y = x[idx1], y[idx1], x[idx2], y[idx2]
        elif is_classifier(self):
            if stratify:
                idx1, idx2 = train_test_split(np.arange(n_samples),test_size=val_ratio, stratify=y, random_state=self.random_state)
            else:
                idx1, idx2 = train_test_split(np.arange(n_samples),test_size=val_ratio, random_state=self.random_state)
            tr_x, tr_y, val_x, val_y = x[idx1], y[idx1], x[idx2], y[idx2]
        
        batch_size = min(batch_size, tr_x.shape[0])
        val_xb = np.dot(val_x, self.beta_)
        if is_regressor(self):
            val_pred = self.shape_fit_.predict(val_xb)
            val_loss = self.shape_fit_.get_loss(val_y, val_pred)
        elif is_classifier(self):
            val_pred = self.shape_fit_.predict_proba(val_xb)[:, 1]
            val_loss = self.shape_fit_.get_loss(val_y, val_pred)

        self_copy = deepcopy(self)
        no_middle_iter_change = 0
        val_loss_middle_iter_best = val_loss
        for middle_iter in range(max_middle_iter):

            m_t = 0 # moving average of the gradient
            v_t = 0 # moving average of the gradient square
            num_updates = 0
            no_inner_iter_change = 0
            theta_0 = self_copy.beta_ 
            train_size = tr_x.shape[0]
            val_loss_inner_iter_best = np.inf
            for inner_iter in range(max_inner_iter):

                shuffle_index = np.arange(tr_x.shape[0])
                np.random.shuffle(shuffle_index)
                tr_x = tr_x[shuffle_index]
                tr_y = tr_y[shuffle_index]

                for iterations in range(train_size // batch_size):

                    num_updates += 1
                    offset = (iterations * batch_size) % train_size
                    batch_xx = tr_x[offset:(offset + batch_size), :]
                    batch_yy = tr_y[offset:(offset + batch_size)]

                    xb = np.dot(batch_xx, theta_0)
                    if is_regressor(self_copy):
                        r = batch_yy - self_copy.shape_fit_.predict(xb).ravel()
                    elif is_classifier(self_copy):
                        r = batch_yy - self_copy.shape_fit_.predict_proba(xb)[:, 1]
                    
                    # gradient
                    dfxb = self_copy.shape_fit_.diff(xb, order=1).ravel()
                    g_t = np.average((- dfxb * r).reshape(-1, 1) * batch_xx, axis=0).reshape(-1, 1)

                    # update the moving average 
                    m_t = beta_1 * m_t + (1 - beta_1) * g_t
                    v_t = beta_2 * v_t + (1 - beta_2) * (g_t * g_t)
                    # calculates the bias-corrected estimates
                    m_cap = m_t / (1 - (beta_1 ** (num_updates)))  
                    v_cap = v_t / (1 - (beta_2 ** (num_updates)))
                    # updates the parameters
                    theta_0 = theta_0 - (learning_rate * m_cap) / (np.sqrt(v_cap) + 1e-8)

                # validation loss
                val_xb = np.dot(val_x, theta_0)
                if is_regressor(self_copy):
                    val_pred = self_copy.shape_fit_.predict(val_xb)
                    val_loss = self_copy.shape_fit_.get_loss(val_y, val_pred)
                elif is_classifier(self_copy):
                    val_pred = self_copy.shape_fit_.predict_proba(val_xb)[:, 1]
                    val_loss = self_copy.shape_fit_.get_loss(val_y, val_pred)
                if verbose:
                    print("Middle iter:", middle_iter + 1, "Inner iter:", inner_iter + 1, "with validation loss:", np.round(val_loss, 5))
                # stop criterion
                if val_loss > val_loss_inner_iter_best - tol:
                    no_inner_iter_change += 1
                else:
                    no_inner_iter_change = 0
                if val_loss < val_loss_inner_iter_best:
                    val_loss_inner_iter_best = val_loss
                
                if no_inner_iter_change >= n_inner_iter_no_change:
                    break
  
            ## normalization
            if np.linalg.norm(theta_0) > 0:
                theta_0 = theta_0 / np.linalg.norm(theta_0)
                if (theta_0[np.argmax(np.abs(theta_0))] < 0):
                    theta_0 = - theta_0

            # ridge update
            self_copy.beta_ = theta_0
            tr_xb = np.dot(tr_x, self_copy.beta_)
            self_copy._estimate_shape(tr_xb, tr_y, np.min(tr_xb), np.max(tr_xb))
            
            val_xb = np.dot(val_x, self_copy.beta_)
            if is_regressor(self_copy):
                val_pred = self_copy.shape_fit_.predict(val_xb)
                val_loss = self_copy.shape_fit_.get_loss(val_y, val_pred)
            elif is_classifier(self_copy):
                val_pred = self_copy.shape_fit_.predict_proba(val_xb)[:, 1]
                val_loss = self_copy.shape_fit_.get_loss(val_y, val_pred)

            if val_loss > val_loss_middle_iter_best - tol:
                no_middle_iter_change += 1
            else:
                no_middle_iter_change = 0
            if val_loss < val_loss_middle_iter_best:
                self.beta_ = self_copy.beta_
                self.shape_fit_ = self_copy.shape_fit_
                val_loss_middle_iter_best = val_loss
            if no_middle_iter_change >= n_middle_iter_no_change:
                break
                
        self = deepcopy(self_copy)

    def visualize(self):

        """draw the fitted projection indices and ridge function
        """

        check_is_fitted(self, "beta_")
        check_is_fitted(self, "shape_fit_")

        xlim_min = - max(np.abs(self.beta_.min() - 0.1), np.abs(self.beta_.max() + 0.1))
        xlim_max = max(np.abs(self.beta_.min() - 0.1), np.abs(self.beta_.max() + 0.1))

        fig = plt.figure(figsize=(12, 4))
        outer = gridspec.GridSpec(1, 2, wspace=0.15)
        inner = gridspec.GridSpecFromSubplotSpec(2, 1, subplot_spec=outer[0], wspace=0.1, hspace=0.1, height_ratios=[6, 1])
        ax1_main = plt.Subplot(fig, inner[0])
        xgrid = np.linspace(self.shape_fit_.xmin, self.shape_fit_.xmax, 100).reshape([-1, 1])
        ygrid = self.shape_fit_.decision_function(xgrid)
        ax1_main.plot(xgrid, ygrid)
        ax1_main.set_xticklabels([])
        ax1_main.set_title("Shape Function", fontsize=12)
        fig.add_subplot(ax1_main)

        ax1_density = plt.Subplot(fig, inner[1])
        xint = ((np.array(self.shape_fit_.bins_[1:]) + np.array(self.shape_fit_.bins_[:-1])) / 2).reshape([-1, 1]).reshape([-1])
        ax1_density.bar(xint, self.shape_fit_.density_, width=xint[1] - xint[0])
        ax1_main.get_shared_x_axes().join(ax1_main, ax1_density)
        ax1_density.set_yticklabels([])
        ax1_density.autoscale()
        fig.add_subplot(ax1_density)

        ax2 = plt.Subplot(fig, outer[1])
        if len(self.beta_) <= 50:
            ax2.barh(np.arange(len(self.beta_)), [beta for beta in self.beta_.ravel()][::-1])
            ax2.set_yticks(np.arange(len(self.beta_)))
            ax2.set_yticklabels(["X" + str(idx + 1) for idx in range(len(self.beta_.ravel()))][::-1])
            ax2.set_xlim(xlim_min, xlim_max)
            ax2.set_ylim(-1, len(self.beta_))
            ax2.axvline(0, linestyle="dotted", color="black")
        else:
            right = np.round(np.linspace(0, np.round(len(self.beta_) * 0.45).astype(int), 5))
            left = len(self.beta_) - 1 - right
            input_ticks = np.unique(np.hstack([left, right])).astype(int)

            ax2.barh(np.arange(len(self.beta_)), [beta for beta in self.beta_.ravel()][::-1])
            ax2.set_yticks(input_ticks)
            ax2.set_yticklabels(["X" + str(idx + 1) for idx in input_ticks][::-1])
            ax2.set_xlim(xlim_min, xlim_max)
            ax2.set_ylim(-1, len(self.beta_))
            ax2.axvline(0, linestyle="dotted", color="black")
        ax2.set_title("Projection Indice", fontsize=12)
        fig.add_subplot(ax2)
        plt.show()


class SimRegressor(BaseSim, RegressorMixin):

    """
    Sim regression.

    Parameters
    ----------
    reg_lambda : float, optional. default=0
        Sparsity strength

    reg_gamma : float or list of float, optional. default=0.1
        Roughness penalty strength of the spline algorithm

    degree : int, optional. default=3
        The order of the spline. Possible values include 1 and 3.

    knot_num : int, optional. default=5
        Number of knots

    random_state : int, optional. default=0
        Random seed
    """

    def __init__(self, reg_lambda=0, reg_gamma=1e-5, knot_num=5, degree=3, random_state=0):

        super(SimRegressor, self).__init__(reg_lambda=reg_lambda,
                                reg_gamma=reg_gamma,
                                knot_num=knot_num,
                                degree=degree,
                                random_state=random_state)

    def _validate_input(self, x, y):

        """method to validate data

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        y : array-like of shape (n_samples,)
            containing the output dataset
        """

        x, y = check_X_y(x, y, accept_sparse=["csr", "csc", "coo"],
                         multi_output=True, y_numeric=True)
        return x, y.ravel()

    def _estimate_shape(self, x, y, xmin, xmax):

        """estimate the ridge function

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        y : array-like of shape (n_samples,)
            containing the output dataset
        xmin : float
            the minimum value of beta ^ x
        xmax : float
            the maximum value of beta ^ x
        """

        self.shape_fit_ = SMSplineRegressor(knot_num=self.knot_num, reg_gamma=self.reg_gamma,
                                xmin=xmin, xmax=xmax, degree=self.degree)
        self.shape_fit_.fit(x, y)

    def predict(self, x):

        """output f(beta^T x) for given samples

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        Returns
        -------
        np.array of shape (n_samples,)
            containing f(beta^T x)
        """
        pred = self.decision_function(x)
        return pred


class SimClassifier(BaseSim, ClassifierMixin):

    """
    Sim classification.

    Parameters
    ----------
    reg_lambda : float, optional. default=0
        Sparsity strength

    reg_gamma : float or list of float, optional. default=0.1
        Roughness penalty strength of the spline algorithm

    degree : int, optional. default=3
        The order of the spline

    knot_num : int, optional. default=5
        Number of knots

    random_state : int, optional. default=0
        Random seed
    """

    def __init__(self, reg_lambda=0, reg_gamma=1e-5, knot_num=5, degree=3, random_state=0):

        super(SimClassifier, self).__init__(reg_lambda=reg_lambda,
                                reg_gamma=reg_gamma,
                                knot_num=knot_num,
                                degree=degree,
                                random_state=random_state)

    def _validate_input(self, x, y):

        """method to validate data

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        y : array-like of shape (n_samples,)
            containing target values
        """

        x, y = check_X_y(x, y, accept_sparse=["csr", "csc", "coo"],
                         multi_output=True)
        if y.ndim == 2 and y.shape[1] == 1:
            y = column_or_1d(y, warn=False)

        self._label_binarizer = LabelBinarizer()
        self._label_binarizer.fit(y)
        self.classes_ = self._label_binarizer.classes_

        y = self._label_binarizer.transform(y) * 1.0
        return x, y.ravel()

    def _estimate_shape(self, x, y, xmin, xmax):

        """estimate the ridge function

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        y : array-like of shape (n_samples,)
            containing the output dataset
        xmin : float
            the minimum value of beta ^ x
        xmax : float
            the maximum value of beta ^ x
        """

        self.shape_fit_ = SMSplineClassifier(knot_num=self.knot_num, reg_gamma=self.reg_gamma,
                                xmin=xmin, xmax=xmax, degree=self.degree)
        self.shape_fit_.fit(x, y)

    def predict_proba(self, x):

        """output probability prediction for given samples

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        Returns
        -------
        np.array of shape (n_samples, 2)
            containing probability prediction
        """

        pred = self.decision_function(x)
        pred_proba = softmax(np.vstack([-pred, pred]).T / 2, copy=False)
        return pred_proba

    def predict(self, x):

        """output binary prediction for given samples

        Parameters
        ---------
        x : array-like of shape (n_samples, n_features)
            containing the input dataset
        Returns
        -------
        np.array of shape (n_samples,)
            containing binary prediction
        """

        pred_proba = self.predict_proba(x)[:, 1]
        return self._label_binarizer.inverse_transform(pred_proba)
