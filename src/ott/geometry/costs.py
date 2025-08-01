# Copyright OTT-JAX
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import abc
import functools
import math
from typing import Any, Callable, Dict, Optional, Tuple

import jax
import jax.numpy as jnp
import jax.tree_util as jtu
import jaxopt
import numpy as np

from ott import math as ott_math
from ott.geometry import regularizers
from ott.math import fixed_point_loop, matrix_square_root
from ott.math import utils as mu

__all__ = [
    "PNormP", "SqPNorm", "Euclidean", "SqEuclidean", "RegTICost", "Cosine",
    "Arccos", "Bures", "UnbalancedBures", "SoftDTW", "Dotp"
]

# TODO(michalk8): norm check
Func = Callable[[jnp.ndarray], float]


@jtu.register_pytree_node_class
class CostFn(abc.ABC):
  """Base class for all costs."""

  @abc.abstractmethod
  def __call__(self, x: jnp.ndarray, y: jnp.ndarray) -> float:
    """Compute cost between :math:`x` and :math:`y`.

    Args:
      x: Array.
      y: Array.

    Returns:
      The cost.
    """

  def barycenter(self, weights: jnp.ndarray,
                 xs: jnp.ndarray) -> Tuple[jnp.ndarray, Any]:
    """Barycentric operator.

    Args:
      weights: Convex set of weights.
      xs: Points.

    Returns:
      A list, whose first element is the barycenter of `xs` using `weights`
      coefficients, followed by auxiliary information on the convergence of
      the algorithm.
    """
    raise NotImplementedError("Barycenter is not implemented.")

  @classmethod
  def _padder(cls, dim: int) -> jnp.ndarray:
    """Create a padding vector of adequate dimension, well-suited to a cost.

    Args:
      dim: Dimensionality of the data.

    Returns:
      The padding vector.
    """
    return jnp.zeros((1, dim))

  def all_pairs(self, x: jnp.ndarray, y: jnp.ndarray) -> jnp.ndarray:
    """Compute matrix of all pairwise costs, including the :attr:`norms <norm>`.

    Args:
      x: Array of shape ``[n, ...]``.
      y: Array of shape ``[m, ...]``.

    Returns:
      Array of shape ``[n, m]`` of cost evaluations.
    """
    return jax.vmap(lambda x_: jax.vmap(lambda y_: self(x_, y_))(y))(x)

  def twist_operator(
      self, vec: jnp.ndarray, dual_vec: jnp.ndarray, variable: bool
  ) -> jnp.ndarray:
    r"""Twist inverse operator of the cost function.

    Given a cost function :math:`c`, the twist operator returns
    :math:`\nabla_{1}c(x, \cdot)^{-1}(z)` if ``variable`` is ``0``,
    and :math:`\nabla_{2}c(\cdot, y)^{-1}(z)` if ``variable`` is ``1``, for
    :math:`x=y` equal to ``vec`` and :math:`z` equal to ``dual_vec``.

    Args:
      vec: ``[p,]`` point at which the twist inverse operator is evaluated.
      dual_vec: ``[q,]`` point to invert by the operator.
      variable: apply twist inverse operator on first (i.e. value set to ``0``
        or equivalently ``False``) or second (``1`` or ``True``) variable.

    Returns:
      A vector.
    """
    raise NotImplementedError("Twist operator is not implemented.")

  def tree_flatten(self):  # noqa: D102
    return (), None

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del aux_data
    return cls(*children)


@jtu.register_pytree_node_class
class TICost(CostFn):
  """Base class for translation invariant (TI) costs.

  Such costs are defined using a function :math:`h`, mapping vectors to
  real-values, to be used as:

  .. math::
    c(x, y) = h(z), z := x - y.

  If that cost function is used to form an Entropic map using the
  :cite:`brenier:91` theorem, then the user should ensure :math:`h` is
  strictly convex, as well as provide the Legendre transform of :math:`h`,
  whose gradient is necessarily the inverse of the gradient of :math:`h`.
  """

  @abc.abstractmethod
  def h(self, z: jnp.ndarray) -> float:
    """TI function acting on difference of :math:`x-y` to output cost.

    Args:
      z: Array of shape ``[d,]``.

    Returns:
      The cost.
    """

  def h_legendre(self, z: jnp.ndarray) -> float:
    """Legendre transform of :func:`h` when it is convex."""
    raise NotImplementedError("Legendre transform of `h` is not implemented.")

  def __call__(self, x: jnp.ndarray, y: jnp.ndarray) -> float:
    """Compute cost as evaluation of :func:`h` on :math:`x-y`."""
    return self.h(x - y)

  def h_transform(
      self,
      f: Func,
      solver: Optional[Callable[[Func, jnp.ndarray, jnp.ndarray, Any],
                                jnp.ndarray]] = None,
  ) -> Callable[[jnp.ndarray, Optional[jnp.ndarray], Any], float]:
    r"""Compute the h-transform of a concave function.

    Return a callable :math:`f_h` defined as:

    .. math::
      f_h(x) = \min_y h(x - y) - f(y)

    This is equivalent, up to a change of variables, :math:`z = x - y`, to
    define

    .. math::
      \min_z h(z) - f(x - z). \\
      \min_z h(z) + \tilde{f}(z, x).

    where :math:`\tilde{f}(z, x) := -f(x - z)`.

    Args:
      f: Concave function.
      solver: Solver with the signature
        ``(func, x_init, **kwargs) -> (sol, aux)``. If :obj:`None`, use
        :func:`~ott.math.lbfgs`.

    Returns:
      The h-transform :math:`f_h` of :math:`f`.
    """
    if solver is None:
      solver = ott_math.lbfgs

    def f_h(
        x: jnp.ndarray,
        x_init: Optional[jnp.ndarray] = None,
        **kwargs: Any
    ) -> float:
      """h-transform of a concave function.

      Args:
        x: Array of shape ``[d,]`` where to evaluate the function.
        x_init: Initialization for optimization. If :obj:`None`, use ``x``.
        kwargs: Keyword arguments for the solver, e.g. maximal iterations or
          tolerance.

      Returns:
        The :math:`h`-transform of :math:`f`, :math:`f_h(x)`.
      """

      def fun(z: jnp.ndarray) -> float:
        return self.h(z) - f(x - z)

      x_init = x if x_init is None else x_init
      z, _ = solver(fun, x_init, **kwargs)
      z = jax.lax.stop_gradient(z)
      return fun(z)

    return f_h

  def twist_operator(
      self, vec: jnp.ndarray, dual_vec: jnp.ndarray, variable: bool
  ) -> jnp.ndarray:
    # Note: when `h` is pair, i.e. h(z) = h(-z), the expressions below coincide
    if variable:
      return vec + jax.grad(self.h_legendre)(-dual_vec)
    return vec - jax.grad(self.h_legendre)(dual_vec)

  def transport_map(self, g: Func) -> Callable[[jnp.ndarray, Any], jnp.ndarray]:
    r"""Get an optimal transport map for a concave function :math:`g`.

    Uses Proposition 1 from :cite:`klein:24` to define an OT map
    :math:`x - (\nabla h^*) \circ \nabla \bar g^h(x)`, where :math:`h^*`
    is the Legendre transform of :math:`h` and :math:`\bar g^h`
    is the :meth:`h_transform` of a concave function :math:`g`.

    Args:
      g: Concave function.

    Returns:
      The transport map with a signature ``(x, **kwargs)``.
    """

    def transport(x: jnp.ndarray, **kwargs: Any) -> jnp.ndarray:
      """Transport points from source to target.

      Args:
        x: Array of shape ``[n, d]``.
        kwargs: Keyword arguments for the output of the
          :meth:`h_transform` method.

      Returns:
        The transported points.
      """
      g_h = functools.partial(self.h_transform(g), **kwargs)
      grad_g_h = jax.vmap(jax.grad(g_h))
      return jax.vmap(
          self.twist_operator, in_axes=[0, 0, None]
      )(x, grad_g_h(x), False)

    return transport

  def barycenter(self, weights: jnp.ndarray,
                 xs: jnp.ndarray) -> Tuple[jnp.ndarray, Any]:
    """Output barycenter of vectors."""
    return jnp.average(xs, weights=weights, axis=0), None


@jtu.register_pytree_node_class
class SqPNorm(TICost):
  r"""Squared p-norm of the difference of two vectors.

  Uses custom implementation of `norm` to avoid `NaN` values when
  differentiating the norm of `x-x`.

  Args:
    p: Power of the p-norm, :math:`\ge 1`.
  """

  def __init__(self, p: float):
    super().__init__()
    self.p = p
    self.q = 1.0 / (1.0 - (1.0 / p)) if p > 1.0 else jnp.inf

  def h(self, z: jnp.ndarray) -> float:  # noqa: D102
    return 0.5 * mu.norm(z, self.p) ** 2

  def h_legendre(self, z: jnp.ndarray) -> float:
    """Legendre transform of :func:`h`.

    For details on the derivation, see e.g., :cite:`boyd:04`, p. 93/94.
    """
    return 0.5 * mu.norm(z, self.q) ** 2

  def tree_flatten(self):  # noqa: D102
    return (), (self.p,)

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del children
    return cls(*aux_data)


@jtu.register_pytree_node_class
class PNormP(TICost):
  r""":math:`p`-norm to the power :math:`p` and divided by :math:`p`.

  Uses custom implementation of `norm` to avoid `NaN` values when
  differentiating the norm of :math:`x-x`.

  Args:
    p: Power of the p-norm in :math:`[1, +\infty)`.
      Note that :func:`h_legendre` is not defined for ``p = 1``.
  """

  def __init__(self, p: float):
    super().__init__()
    self.p = p
    self.q = 1.0 / (1.0 - (1.0 / p)) if p > 1.0 else jnp.inf

  def h(self, z: jnp.ndarray) -> float:  # noqa: D102
    return mu.norm(z, self.p) ** self.p / self.p

  def h_legendre(self, z: jnp.ndarray) -> float:  # noqa: D102
    # not defined for `p=1`
    return mu.norm(z, self.q) ** self.q / self.q

  def tree_flatten(self):  # noqa: D102
    return (), (self.p,)

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del children
    return cls(*aux_data)


@jtu.register_pytree_node_class
class EuclideanP(TICost):
  r""":math:`p`-power of Euclidean norm.

  Uses custom implementation of `norm` to avoid `NaN` values when
  differentiating the norm of :math:`x-x`.

  Args:
    p: Power used to raise Euclidean norm, in :math:`[1, +\infty)`.
  """

  def __init__(self, p: float):
    super().__init__()
    self.p = p

  def h(self, z: jnp.ndarray) -> float:  # noqa: D102
    return mu.norm(z, ord=2) ** self.p

  def tree_flatten(self):  # noqa: D102
    return (), (self.p,)

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del children
    return cls(*aux_data)


@jtu.register_pytree_node_class
class Dotp(CostFn):
  r"""Negative Dot-product cost.

  Should yield similar results to :class:`~ott.geometry.costs.SqEuclidean`.

  .. math::
    c(x,y) = - \langle x, y\rangle
  """

  def __call__(self, x: jnp.ndarray, y: jnp.ndarray) -> float:  # noqa: D102
    return -jnp.vdot(x, y)

  def twist_operator(self, vec, dual_vec, variable) -> jnp.ndarray:
    """Twist operator for negative dot-product cost."""
    return -vec if variable else -dual_vec

  def norm(self, x: jnp.ndarray) -> jnp.ndarray:
    """Compute squared Euclidean norm for vector. Only used for rescaling."""
    return jnp.sum(x ** 2, axis=-1)


@jtu.register_pytree_node_class
class RegTICost(TICost):
  r"""Regularized translation-invariant cost.

  .. math::
    \frac{\rho}{2}\|\cdot\|_2^2 + \lambda \text{regularizer}\left(\cdot\right)

  Args:
    regularizer: Regularization function.
    lam: Strength of the regularization.
    rho: Strength of the quadratic part.
  """

  def __init__(
      self,
      regularizer: regularizers.ProximalOperator,
      lam: float = 1.0,
      *,
      rho: float = 1.0,
  ):
    super().__init__()
    self.regularizer = regularizers.PostComposition(regularizer, alpha=lam)
    self._h = regularizers.Regularization(
        self.regularizer,
        a=None,
        rho=rho,
    )

  def h(self, z: jnp.ndarray) -> float:  # noqa: D102
    return self._h(z)

  def h_legendre(self, z: jnp.ndarray) -> float:  # noqa: D102
    """Legendre transform of :func:`h`.

    This function uses :class:`~jax.custom_vjp` to apply Danskin's theorem
    :cite:`danskin:67` when differentiating.

    Args:
      z: Array of shape ``[d,]``.

    Returns:
      The value.
    """

    @jax.custom_vjp
    def fn(z: jnp.ndarray) -> float:
      out, _ = fwd(z)
      return out

    def fwd(z: jnp.ndarray) -> Tuple[float, jnp.ndarray]:
      q = self.regularizer.prox(z)
      return jnp.dot(q, z) - self.h(q), q

    def bwd(q: jnp.ndarray, g: jnp.ndarray) -> Tuple[jnp.ndarray]:
      return jnp.dot(g, q),

    fn.defvjp(fwd, bwd)
    return fn(z)

  def h_transform(
      self,
      f: Func,
  ) -> Callable[[jnp.ndarray, Optional[jnp.ndarray], Any], float]:
    r"""Compute the h-transform of a concave function.

    Return a callable :math:`f_h` defined as:

    .. math::
      f_h(x) = \min_y h(x - y) - f(y)

    This is equivalent, up to a change of variables, :math:`z = x - y`, to
    define

    .. math::
      \min_z h(z) - f(x - z). \\
      \min_z h(z) + \tilde{f}(z, x).

    where :math:`\tilde{f}(z, x) := -f(x - z)`.

    By contrast to the :meth:`TICost.h_transform <TICost.h_transform>`, this is
    exploiting the structure of :meth:`h` and requires having access to its
    proximal operator :math:`\prox_{\tau h}(x)`. Under the hood, this is
    solved using :class:`proximal gradient descent <jaxopt.ProximalGradient>`.

    Args:
      f: Concave function.

    Returns:
      The h-transform :math:`f_h` of :math:`f`.
    """

    def f_h(
        x: jnp.ndarray,
        x_init: Optional[jnp.ndarray] = None,
        **kwargs: Any
    ) -> float:
      """h-transform of a concave function.

      Args:
        x: Array of shape ``[d,]`` where to evaluate the function.
        x_init: Initial estimate. If :obj:`None`, use ``x``.
        kwargs: Keyword arguments for :class:`~jaxopt.ProximalGradient`.

      Returns:
        The output :math:`f_h(x)`.
      """
      solver = jaxopt.ProximalGradient(
          fun=lambda z, x: -f(x - z),
          prox=lambda x, h, tau: h.prox(x, tau),
          **kwargs,
      )
      if x_init is None:
        x_init = x
      z = solver.run(x_init, self._h, x=x).params
      z = jax.lax.stop_gradient(z)
      return self.h(z) - f(x - z)

    return f_h

  @property
  def lam(self) -> float:
    """Strength of the regularization.

    Alias for :attr:`~ott.geometry.regularizers.PostComposition.alpha`.
    """
    return self.regularizer.alpha

  @property
  def rho(self) -> float:
    r"""Strength of the quadratic part :math:`\frac{\rho}{2}\|\cdot\|_2^2`."""
    return self._h.rho

  def tree_flatten(self):  # noqa: D102
    return (self.regularizer.f, self.lam, self.rho), {}

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    f, lam, rho = children
    return cls(f, lam=lam, rho=rho, **aux_data)


@jtu.register_pytree_node_class
class Euclidean(CostFn):
  """Euclidean distance.

  Note that the Euclidean distance is not cast as a
  :class:`~ott.geometry.costs.TICost`, since this would correspond to :math:`h`
  being :func:`jax.numpy.linalg.norm`, whose gradient is not invertible,
  because the function is not strictly convex (it is linear on rays).
  """

  def __call__(self, x: jnp.ndarray, y: jnp.ndarray) -> float:
    """Compute sq. Euclidean distance using a custom jvp implementation.

    Here we use a custom jvp implementation for the norm that does not yield
    `NaN` gradients when differentiating the norm of `(x-x)`, but defaults
    instead to zero, using a `custom_jvp` rule.
    """
    return mu.norm(x - y)


@jtu.register_pytree_node_class
class SqEuclidean(TICost):
  r"""Squared Euclidean distance.

  Implemented as a translation invariant cost, :math:`h(z) = \|z\|^2`.
  """

  def norm(self, x: jnp.ndarray) -> jnp.ndarray:
    """Compute squared Euclidean norm for vector."""
    return jnp.sum(x ** 2, axis=-1)

  def __call__(self, x: jnp.ndarray, y: jnp.ndarray) -> float:
    """Compute minus twice the dot-product between vectors."""
    cross_term = -2.0 * jnp.vdot(x, y)
    return self.norm(x) + self.norm(y) + cross_term

  def h(self, z: jnp.ndarray) -> float:  # noqa: D102
    return jnp.sum(z ** 2)

  def h_legendre(self, z: jnp.ndarray) -> float:  # noqa: D102
    return 0.25 * jnp.sum(z ** 2)

  def barycenter(self, weights: jnp.ndarray,
                 xs: jnp.ndarray) -> Tuple[jnp.ndarray, Any]:
    """Output barycenter of vectors when using squared-Euclidean distance."""
    return jnp.average(xs, weights=weights, axis=0), None


@jtu.register_pytree_node_class
class Cosine(CostFn):
  """Cosine distance cost function.

  Args:
    ridge: Ridge regularization.
  """

  def __init__(self, ridge: float = 1e-8):
    super().__init__()
    self._ridge = ridge

  def __call__(self, x: jnp.ndarray, y: jnp.ndarray) -> float:
    """Cosine distance between vectors, denominator regularized with ridge."""
    x_norm = jnp.linalg.norm(x, axis=-1)
    y_norm = jnp.linalg.norm(y, axis=-1)
    cosine_similarity = jnp.vdot(x, y) / (x_norm * y_norm + self._ridge)
    return 1.0 - cosine_similarity

  @classmethod
  def _padder(cls, dim: int) -> jnp.ndarray:
    return jnp.ones((1, dim))


@jtu.register_pytree_node_class
class Arccos(CostFn):
  r"""Arc-cosine cost function :cite:`cho:09`.

  The cost is implemented as:

  .. math::
    c_n(x, y) = -\log(\frac{1}{\pi} \|x\|^n \|y\|^n J_n(\theta))

  where :math:`\theta := \arccos(\frac{x \cdot y}{\|x\| \|y\|})` and
  :math:`J_n(\theta) := (-1)^n (\sin \theta)^{2n + 1}
  (\frac{1}{\sin \theta}\frac{\partial}{\partial \theta})^n
  (\frac{\pi - \theta}{\sin \theta})`.

  Args:
    n: Order of the kernel. For :math:`n > 2`, successive applications of
      :func:`~jax.grad` are used to compute the :math:`J_n(\theta)`.
    ridge: Ridge regularization.
  """

  def __init__(self, n: int, ridge: float = 1e-8):
    self.n = n
    self._ridge = ridge

  def __call__(self, x: jnp.ndarray, y: jnp.ndarray):  # noqa: D102
    x_norm = jnp.linalg.norm(x, axis=-1)
    y_norm = jnp.linalg.norm(y, axis=-1)
    cosine_similarity = jnp.vdot(x, y) / (x_norm * y_norm + self._ridge)
    theta = jnp.arccos(cosine_similarity)

    if self.n == 0:
      m = 1.0 - theta / jnp.pi
    elif self.n == 1:
      j = jnp.sin(theta) + (jnp.pi - theta) * jnp.cos(theta)
      m = (x_norm * y_norm) * (j / jnp.pi)
    elif self.n == 2:
      j = 3.0 * jnp.sin(theta) * jnp.cos(theta) + (jnp.pi - theta) * (
          1.0 + 2.0 * jnp.cos(theta) ** 2
      )
      m = (x_norm * y_norm) ** 2 * (j / jnp.pi)
    else:
      j = self._j(theta)  # less optimized version using autodiff
      m = (x_norm * y_norm) ** self.n * (j / jnp.pi)

    return -jnp.log(m + self._ridge)

  @jax.jit
  def _j(self, theta: float) -> float:

    def f(t: float, i: int) -> float:
      if i == 0:
        return (jnp.pi - t) / jnp.sin(t)
      return jax.grad(f)(t, i - 1) / jnp.sin(t)

    n = self.n
    return (-1) ** n * jnp.sin(theta) ** (2.0 * n + 1.0) * f(theta, n)

  def tree_flatten(self):  # noqa: D102
    return [], {"n": self.n, "ridge": self._ridge}

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del children
    return cls(**aux_data)


@jtu.register_pytree_node_class
class Bures(CostFn):
  """Bures distance between a pair of (mean, covariance matrix).

  Args:
    dimension: Dimensionality of the data.
    sqrtm_kw: Dictionary of keyword arguments to control the
      behavior of inner calls to :func:`~ott.math.matrix_square_root.sqrtm`.
  """

  def __init__(self, dimension: int, sqrtm_kw: Optional[Dict[str, Any]] = None):
    super().__init__()
    self._dimension = dimension
    self._sqrtm_kw = {} if sqrtm_kw is None else sqrtm_kw

  def norm(self, x: jnp.ndarray) -> jnp.ndarray:
    """Compute norm of Gaussian, sq. 2-norm of mean + trace of covariance."""
    mean, cov = x_to_means_and_covs(x, self._dimension)
    norm = jnp.sum(mean ** 2, axis=-1)
    norm += jnp.trace(cov, axis1=-2, axis2=-1)
    return norm

  def __call__(self, x: jnp.ndarray, y: jnp.ndarray) -> float:
    """Compute - 2 x Bures dot-product."""
    mean_x, cov_x = x_to_means_and_covs(x, self._dimension)
    mean_y, cov_y = x_to_means_and_covs(y, self._dimension)
    mean_dot_prod = jnp.vdot(mean_x, mean_y)
    sq_x = matrix_square_root.sqrtm(cov_x, self._dimension, **self._sqrtm_kw)[0]
    sq_x_y_sq_x = jnp.matmul(sq_x, jnp.matmul(cov_y, sq_x))
    sq__sq_x_y_sq_x = matrix_square_root.sqrtm(
        sq_x_y_sq_x, self._dimension, **self._sqrtm_kw
    )[0]
    cross_term = -2.0 * (
        mean_dot_prod + jnp.trace(sq__sq_x_y_sq_x, axis1=-2, axis2=-1)
    )
    return self.norm(x) + self.norm(y) + cross_term

  def covariance_fixpoint_iter(
      self,
      covs: jnp.ndarray,
      weights: jnp.ndarray,
      tolerance: float = 1e-4,
      sqrtm_kw: Optional[Dict[str, Any]] = None,
      **kwargs: Any
  ) -> jnp.ndarray:
    """Iterate fix-point updates to compute barycenter of Gaussians.

    Args:
      covs: [batch, d^2] covariance matrices
      weights: simplicial weights (non-negative, sum to 1)
      tolerance: tolerance of the fixed-point procedure. That tolerance is
        applied to the Frobenius norm (normalized by total size)
        of two successive iterations of the algorithm
      sqrtm_kw: keyword arguments for :func:`~ott.math.matrix_square_root.sqrtm`
      kwargs: keyword arguments for the outer fixed-point iteration

    Returns:
      List containing Weighted Bures average of the covariance matrices, and
      vector of (normalized) 2-norms of successive differences between iterates,
      to monitor convergence.
    """
    sqrtm_kw = {} if sqrtm_kw is None else sqrtm_kw
    # Pop values or set defaults for fixed-point loop.
    min_iterations = kwargs.pop("min_iterations", 1)
    max_iterations = kwargs.pop("max_iterations", 100)
    inner_iterations = kwargs.pop("inner_iterations", 5)

    @functools.partial(jax.vmap, in_axes=[None, 0, 0])
    def scale_covariances(
        cov_sqrt: jnp.ndarray, cov: jnp.ndarray, weight: jnp.ndarray
    ) -> jnp.ndarray:
      """Rescale covariance in barycenter step."""
      return weight * matrix_square_root.sqrtm_only((cov_sqrt @ cov) @ cov_sqrt,
                                                    **sqrtm_kw)

    def cond_fn(iteration: int, constants: Tuple[Any, ...], state) -> bool:
      del constants
      _, diffs = state
      return diffs[iteration // inner_iterations] > tolerance

    def body_fn(
        iteration: int, constants: Tuple[Any, ...],
        state: Tuple[jnp.ndarray, float], compute_error: bool
    ) -> Tuple[jnp.ndarray, float]:
      del constants, compute_error
      cov, diffs = state
      cov_sqrt, cov_inv_sqrt, _ = matrix_square_root.sqrtm(cov, **sqrtm_kw)
      scaled_cov = jnp.linalg.matrix_power(
          jnp.sum(scale_covariances(cov_sqrt, covs, weights), axis=0), 2
      )
      next_cov = (cov_inv_sqrt @ scaled_cov) @ cov_inv_sqrt
      diff = jnp.sum((next_cov - cov) ** 2) / jnp.prod(jnp.array(cov.shape))
      diffs = diffs.at[iteration // inner_iterations].set(diff)
      return next_cov, diffs

    def init_state() -> Tuple[jnp.ndarray, float]:
      cov_init = jnp.eye(self._dimension)
      diffs = -jnp.ones(math.ceil(max_iterations / inner_iterations))
      return cov_init, diffs

    cov, diffs = fixed_point_loop.fixpoint_iter(
        cond_fn=cond_fn,
        body_fn=body_fn,
        min_iterations=min_iterations,
        max_iterations=max_iterations,
        inner_iterations=inner_iterations,
        constants=(),
        state=init_state(),
    )
    return cov, diffs

  def barycenter(
      self,
      weights: jnp.ndarray,
      xs: jnp.ndarray,
      tolerance: float = 1e-4,
      sqrtm_kw: Optional[Dict[str, Any]] = None,
      **kwargs: Any
  ) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """Compute the Bures barycenter of weighted Gaussian distributions.

    Implements the fixed point approach proposed in :cite:`alvarez-esteban:16`
    for the computation of the mean and the covariance of the barycenter of
    weighted Gaussian distributions.

    Args:
      weights: The barycentric weights.
      xs: The points to be used in the computation of the barycenter, where
        each point is described by a concatenation of the mean and the
        covariance (raveled).
      tolerance: convergence tolerance to control the termination of the
        algorithm.
      sqrtm_kw: Arguments passed on to the
        :func:`~ott.math.matrix_square_root.sqrtm` function used within
        :meth:`covariance_fixpoint_iter`. This defines the precision
        (in terms of convergence threshold, and number of iterations) of the
        matrix square root calls that are used at each outer iteration of
        the computation of Gaussian barycenters. These values are, by default,
        the same as those used to define the Bures cost object itself.
      kwargs: Passed on to :meth:`covariance_fixpoint_iter`, to specify the
        number of iterations and tolerance of the fixed-point iteration of the
        barycenter routine, by parameterizing `tolerance` and other relevant
        arguments passed on to :func:`~ott.math.fixed_point_loop.fixpoint_iter`,
        namely `min_iterations`, `max_iterations` and `inner_iterations`.

    Returns:
      A list holding a concatenation of the mean and the raveled covariance
      of the barycenter as its first element, followed by a vector of
      norms of successive differences in iterates.
    """
    # Ensure that barycentric weights sum to 1.
    weights = weights / jnp.sum(weights)
    mus, covs = x_to_means_and_covs(xs, self._dimension)
    mu_bary = jnp.sum(weights[:, None] * mus, axis=0)
    cov_bary, diffs = self.covariance_fixpoint_iter(
        covs=covs,
        weights=weights,
        tolerance=tolerance,
        sqrtm_kw=sqrtm_kw if sqrtm_kw is not None else self._sqrtm_kw,
        **kwargs
    )
    return mean_and_cov_to_x(mu_bary, cov_bary, self._dimension), diffs

  @classmethod
  def _padder(cls, dim: int) -> jnp.ndarray:
    dimension = int((-1 + math.sqrt(1 + 4 * dim)) / 2)
    padding = mean_and_cov_to_x(
        jnp.zeros((dimension,)), jnp.eye(dimension), dimension
    )
    return padding[jnp.newaxis, :]

  def tree_flatten(self):  # noqa: D102
    return (), (self._dimension, self._sqrtm_kw)

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del children
    return cls(*aux_data)


@jtu.register_pytree_node_class
class UnbalancedBures(CostFn):
  """Unbalanced Bures distance between two triplets of `(mass, mean, cov)`.

  This cost uses the notation defined in :cite:`janati:20`, eq. 37, 39, 40.

  Args:
    dimension: Dimensionality of the data.
    sigma: Entropic regularization.
    gamma: KL-divergence regularization for the marginals.
    kwargs: Keyword arguments for :func:`~ott.math.matrix_square_root.sqrtm`.
  """

  def __init__(
      self,
      dimension: int,
      *,
      sigma: float = 1.0,
      gamma: float = 1.0,
      **kwargs: Any,
  ):
    super().__init__()
    self._dimension = dimension
    self._sigma = sigma
    self._gamma = gamma
    self._sqrtm_kw = kwargs

  def norm(self, x: jnp.ndarray) -> jnp.ndarray:
    """Compute norm of Gaussian for unbalanced Bures.

    Args:
      x: Array of shape ``[n_points + n_points + n_dim ** 2,]``, potentially
        batched, corresponding to the raveled mass, means and the covariance
        matrix.

    Returns:
      The norm, array of shape ``[]`` or ``[batch,]`` in the batched case.
    """
    return self._gamma * x[..., 0]

  def __call__(self, x: jnp.ndarray, y: jnp.ndarray) -> float:
    """Compute dot-product for unbalanced Bures.

    Args:
      x: Array of shape ``[n_points + n_points + n_dim ** 2,]``
        corresponding to the raveled mass, means and the covariance matrix.
      y: Array of shape ``[n_points + n_points + n_dim ** 2,]``
        corresponding to the raveled mass, means and the covariance matrix.

    Returns:
      The cost.
    """
    # Sets a few constants
    gam = self._gamma
    sig2 = self._sigma ** 2
    lam = sig2 + gam / 2.0
    tau = gam / (2.0 * lam)

    # Extracts mass, mean vector, covariance matrices
    mass_x, mass_y = x[0], y[0]
    mean_x, cov_x = x_to_means_and_covs(x[1:], self._dimension)
    mean_y, cov_y = x_to_means_and_covs(y[1:], self._dimension)

    diff_means = mean_x - mean_y

    # Identity matrix of suitable size
    iden = jnp.eye(self._dimension)

    # Creates matrices needed in the computation
    tilde_a = 0.5 * gam * (iden - lam * jnp.linalg.inv(cov_x + lam * iden))
    tilde_b = 0.5 * gam * (iden - lam * jnp.linalg.inv(cov_y + lam * iden))

    tilde_a_b = jnp.matmul(tilde_a, tilde_b)
    c_mat = matrix_square_root.sqrtm(
        1 / tau * tilde_a_b + 0.25 * (sig2 ** 2) * iden, **self._sqrtm_kw
    )[0]
    c_mat -= 0.5 * sig2 * iden

    # Computes log determinants (their sign should be >0).
    sldet_c, ldet_c = jnp.linalg.slogdet(c_mat)
    sldet_t_ab, ldet_t_ab = jnp.linalg.slogdet(tilde_a_b)
    sldet_ab, ldet_ab = jnp.linalg.slogdet(jnp.matmul(cov_x, cov_y))
    sldet_c_ab, ldet_c_ab = jnp.linalg.slogdet(c_mat - 2.0 * tilde_a_b / gam)

    # Gathers all these results to compute log total mass of transport
    log_m_pi = (0.5 * self._dimension * sig2 / (gam + sig2)) * jnp.log(sig2)
    log_m_pi += (1.0 / (tau + 1.0)) * (
        jnp.log(mass_x) + jnp.log(mass_y) + ldet_c + 0.5 *
        (tau * ldet_t_ab - ldet_ab)
    )
    log_m_pi += -jnp.sum(
        diff_means * jnp.linalg.solve(cov_x + cov_y + lam * iden, diff_means)
    ) / (2.0 * (tau + 1.0))
    log_m_pi += -0.5 * ldet_c_ab

    # if all logdet signs are 1, output value, nan otherwise
    pos_signs = (sldet_c + sldet_c_ab + sldet_ab + sldet_t_ab) == 4

    cross_term = jax.lax.cond(
        pos_signs, lambda: 2 * sig2 * mass_x * mass_y - 2 *
        (sig2 + gam) * jnp.exp(log_m_pi), lambda: jnp.nan
    )
    return self.norm(x) + self.norm(y) + cross_term

  def tree_flatten(self):  # noqa: D102
    return (), (self._dimension, self._sigma, self._gamma, self._sqrtm_kw)

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del children
    dim, sigma, gamma, kwargs = aux_data
    return cls(dim, sigma=sigma, gamma=gamma, **kwargs)


@jtu.register_pytree_node_class
class SoftDTW(CostFn):
  """Soft dynamic time warping (DTW) cost :cite:`cuturi:17`.

  Args:
    gamma: Smoothing parameter :math:`> 0` for the soft-min operator.
    ground_cost: Ground cost function. If ``None``,
      use :class:`~ott.geometry.costs.SqEuclidean`.
    debiased: Whether to compute the debiased soft-DTW :cite:`blondel:21`.
  """

  def __init__(
      self,
      gamma: float,
      ground_cost: Optional[CostFn] = None,
      debiased: bool = False
  ):
    self.gamma = gamma
    self.ground_cost = SqEuclidean() if ground_cost is None else ground_cost
    self.debiased = debiased

  def __call__(self, x: jnp.ndarray, y: jnp.ndarray) -> float:  # noqa: D102
    c_xy = self._soft_dtw(x, y)
    if self.debiased:
      return c_xy - 0.5 * (self._soft_dtw(x, x) + self._soft_dtw(y, y))
    return c_xy

  def _soft_dtw(self, t1: jnp.ndarray, t2: jnp.ndarray) -> float:

    def body(
        carry: Tuple[jnp.ndarray, jnp.ndarray],
        current_antidiagonal: jnp.ndarray
    ) -> Tuple[Tuple[jnp.ndarray, jnp.ndarray], jnp.ndarray]:
      # modified from: https://github.com/khdlr/softdtw_jax
      two_ago, one_ago = carry

      diagonal, right, down = two_ago[:-1], one_ago[:-1], one_ago[1:]
      best = mu.softmin(
          jnp.stack([diagonal, right, down], axis=-1), self.gamma, axis=-1
      )

      next_row = best + current_antidiagonal
      next_row = jnp.pad(next_row, (1, 0), constant_values=jnp.inf)

      return (one_ago, next_row), next_row

    t1 = t1[:, None] if t1.ndim == 1 else t1
    t2 = t2[:, None] if t2.ndim == 1 else t2
    dist = self.ground_cost.all_pairs(t1, t2)

    n, m = dist.shape
    if n < m:
      dist = dist.T
      n, m = m, n

    model_matrix = jnp.full((n + m - 1, n), fill_value=jnp.inf)
    mask = np.tri(n + m - 1, n, k=0, dtype=bool)
    mask = mask & mask[::-1, ::-1]
    model_matrix = model_matrix.T.at[mask.T].set(dist.ravel()).T

    init = (
        jnp.pad(model_matrix[0], (1, 0), constant_values=jnp.inf),
        jnp.pad(
            model_matrix[1] + model_matrix[0, 0], (1, 0),
            constant_values=jnp.inf
        )
    )

    (_, carry), _ = jax.lax.scan(body, init, model_matrix[2:])
    return carry[-1]

  def tree_flatten(self):  # noqa: D102
    return (self.gamma, self.ground_cost), {"debiased": self.debiased}

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    return cls(*children, **aux_data)


def x_to_means_and_covs(x: jnp.ndarray,
                        dimension: int) -> Tuple[jnp.ndarray, jnp.ndarray]:
  """Extract means and covariance matrices of Gaussians from raveled vector.

  Args:
    x: [num_gaussians, dimension, (1 + dimension)] array of concatenated means
      and covariances (raveled) dimension: the dimension of the Gaussians.
    dimension: Dimensionality of the Gaussians.

  Returns:
    Means and covariances of shape ``[num_gaussian, dimension]``.
  """
  x = jnp.atleast_2d(x)
  means = x[:, :dimension]
  covariances = jnp.reshape(
      x[:, dimension:dimension + dimension ** 2], (-1, dimension, dimension)
  )
  return jnp.squeeze(means), jnp.squeeze(covariances)


def mean_and_cov_to_x(
    mean: jnp.ndarray, covariance: jnp.ndarray, dimension: int
) -> jnp.ndarray:
  """Ravel a Gaussian's mean and covariance matrix to d(1 + d) vector."""
  return jnp.concatenate(
      (mean, jnp.reshape(covariance, (dimension * dimension)))
  )
