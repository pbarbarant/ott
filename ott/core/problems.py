# coding=utf-8
# Copyright 2021 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Functions to manipulate the problem(s) or regularized optimal transport."""

from typing import Callable, Optional, Tuple
import jax
import jax.numpy as jnp
from ott.geometry import geometry

LossTerm = Callable[[jnp.ndarray], jnp.ndarray]
Loss = Tuple[Tuple[LossTerm, LossTerm], Tuple[LossTerm, LossTerm]]


@jax.tree_util.register_pytree_node_class
class LinearProblem:
  """Holds the definition of a linear regularized OT problem and some tools."""

  def __init__(self,
               geom: geometry.Geometry,
               a: Optional[jnp.ndarray] = None,
               b: Optional[jnp.ndarray] = None,
               tau_a: float = 1.0,
               tau_b: float = 1.0):
    """Initializes the LinearProblem.

    min_P<C, P> - eps H(P), s.t P.1 = a, Pt.1 = b.

    Args:
      geom: the geometry.Geometry object defining the ground geometry / cost of
        the linear problem.
      a: jnp.ndarray[n] representing the first marginal. If None, it will be
        uniform.
      b: jnp.ndarray[n] representing the first marginal. If None, it will be
        uniform.
      tau_a: if lower that 1.0, defines how much unbalanced the problem is on
        the first marginal.
      tau_b: if lower that 1.0, defines how much unbalanced the problem is on
        the second marginal.
    """
    self.geom = geom
    self._a = a
    self._b = b
    self.tau_a = tau_a
    self.tau_b = tau_b

  def tree_flatten(self):
    return ([self.geom, self._a, self._b],
            {'tau_a': self.tau_a, 'tau_b': self.tau_b})

  @classmethod
  def tree_unflatten(cls, aux_data, children):
    return cls(*children, **aux_data)

  @property
  def a(self):
    num_a = self.geom.shape[0]
    return jnp.ones((num_a,)) / num_a if self._a is None else self._a

  @property
  def b(self):
    num_b = self.geom.shape[1]
    return jnp.ones((num_b,)) / num_b if self._b is None else self._b

  @property
  def is_balanced(self):
    return self.tau_a == 1.0 and self.tau_b == 1.0

  @property
  def epsilon(self):
    return self.geom.epsilon

  def marginal_error(self,
                     f_u: jnp.ndarray,
                     g_v: jnp.ndarray,
                     norm_error: int,
                     lse_mode: bool) -> jnp.ndarray:
    """Given two potentials (or scalings), computes marginal error.

    Args:
      f_u: jnp.ndarray, potential or scaling
      g_v: jnp.ndarray, potential or scaling
      norm_error: int, p-norm used to compute error.
      lse_mode: True if log-sum-exp operations, False if kernel vector producs.

    Returns:
      a positive number quantifying how well the reconstructed marginals are.
    """
    if self.is_balanced:
      return self.geom.error(f_u, g_v, self.b, 0, norm_error, lse_mode)

    # In the unbalanced case, we compute the norm of the gradient.
    # the gradient is equal to the marginal of the current plan minus
    # the gradient of < z, rho_z(exp^(-h/rho_z) -1> where z is either a or b
    # and h is either f or g. Note this is equal to z if rho_z → inf, which
    # is the case when tau_z → 1.0
    if lse_mode:
      grad_a = grad_of_marginal_fit(self.a, f_u, self.tau_a, self.epsilon)
      grad_b = grad_of_marginal_fit(self.b, g_v, self.tau_b, self.epsilon)
    else:
      u = self.geom.potential_from_scaling(f_u)
      v = self.geom.potential_from_scaling(g_v)
      grad_a = grad_of_marginal_fit(self.a, u, self.tau_a, self.epsilon)
      grad_b = grad_of_marginal_fit(self.b, v, self.tau_b, self.epsilon)
    err = self.geom.error(f_u, g_v, grad_a, 1, norm_error, lse_mode)
    err += self.geom.error(f_u, g_v, grad_b, 0, norm_error, lse_mode)
    return err

  def ent_reg_cost(
      self, f: jnp.ndarray, g: jnp.ndarray, lse_mode: bool) -> jnp.ndarray:
    r"""Computes objective of regularized OT given dual solutions ``f``, ``g``.

    The objective is evaluated for dual solution ``f`` and ``g``, using inputs
    ``geom``, ``a`` and ``b``, in addition to parameters ``tau_a``, ``tau_b``.
    Situations where ``a`` or ``b`` have zero coordinates are reflected in
    minus infinity entries in their corresponding dual potentials. To avoid NaN
    that may result when multiplying 0's by infinity values, ``jnp.where`` is
    used to cancel these contributions.

    Args:
      f: jnp.ndarray, potential
      g: jnp.ndarray, potential
      lse_mode: bool, whether to compute total mass in lse or kernel mode.

    Returns:
      a float, the regularized transport cost.
    """
    supp_a = self.a > 0
    supp_b = self.b > 0
    fa = self.geom.potential_from_scaling(self.a)
    if self.tau_a == 1.0:
      div_a = jnp.sum(jnp.where(supp_a, self.a * (f - fa), 0.0))
    else:
      rho_a = self.epsilon * (self.tau_a / (1 - self.tau_a))
      div_a = -jnp.sum(
          jnp.where(supp_a, self.a * phi_star(-(f - fa), rho_a), 0.0))

    gb = self.geom.potential_from_scaling(self.b)
    if self.tau_b == 1.0:
      div_b = jnp.sum(jnp.where(supp_b, self.b * (g - gb), 0.0))
    else:
      rho_b = self.epsilon * (self.tau_b / (1 - self.tau_b))
      div_b = -jnp.sum(
          jnp.where(supp_b, self.b * phi_star(-(g - gb), rho_b), 0.0))

    # Using https://arxiv.org/pdf/1910.12958.pdf (24)
    if lse_mode:
      total_sum = jnp.sum(self.geom.marginal_from_potentials(f, g))
    else:
      u = self.geom.scaling_from_potential(f)
      v = self.geom.scaling_from_potential(g)
      total_sum = jnp.sum(self.geom.marginal_from_scalings(u, v))
    return div_a + div_b + self.epsilon * (
        jnp.sum(self.a) * jnp.sum(self.b) - total_sum)

  def get_transport_functions(self, lse_mode: bool):
    """Instantiates useful functions from geometry depending on lse_mode."""
    geom = self.geom
    if lse_mode:
      marginal_a = lambda f, g: geom.marginal_from_potentials(f, g, 1)
      marginal_b = lambda f, g: geom.marginal_from_potentials(f, g, 0)
      app_transport = geom.apply_transport_from_potentials
    else:
      marginal_a = lambda f, g: geom.marginal_from_scalings(
          geom.scaling_from_potential(f), geom.scaling_from_potential(g), 1)
      marginal_b = lambda f, g: geom.marginal_from_scalings(
          geom.scaling_from_potential(f), geom.scaling_from_potential(g), 0)
      app_transport = lambda f, g, z, axis: geom.apply_transport_from_scalings(
          geom.scaling_from_potential(f),
          geom.scaling_from_potential(g), z, axis)
    return marginal_a, marginal_b, app_transport


def phi_star(h: jnp.ndarray, rho: float) -> jnp.ndarray:
  """Legendre transform of KL, https://arxiv.org/pdf/1910.12958.pdf p.9."""
  return rho * (jnp.exp(h / rho) - 1)


# TODO(cuturi): use jax.grad directly.
def derivative_phi_star(f: jnp.ndarray, rho: float) -> jnp.ndarray:
  """Derivative of Legendre transform of phi_starKL, see phi_star."""
  return jnp.exp(f / rho)


def grad_of_marginal_fit(c, h, tau, epsilon):
  """Computes grad of terms linked to marginals in objective.

  Computes gradient w.r.t. f ( or g) of terms in
  https://arxiv.org/pdf/1910.12958.pdf, left-hand-side of Eq. 15
  (terms involving phi_star)

  Args:
    c: jnp.ndarray, first target marginal (either a or b in practice)
    h: jnp.ndarray, potential (either f or g in practice)
    tau: float, strength (in ]0,1]) of regularizer w.r.t. marginal
    epsilon: regularization

  Returns:
    a vector of the same size as c or h
  """
  if tau == 1.0:
    return c
  else:
    rho = epsilon * tau / (1 - tau)
    return jnp.where(c > 0, c * derivative_phi_star(-h, rho), 0.0)


def make_square_loss():
  return (lambda x: x ** 2, lambda y: y ** 2), (lambda x: x, lambda y: 2.0 * y)


def make_kl_loss(clipping_value: float = 1e-8):

  return (
      (lambda x: -jax.scipy.special.entr(x) - x, lambda y: y),
      (lambda x: x, lambda y: jnp.log(jnp.clip(y, clipping_value)))
  )


@jax.tree_util.register_pytree_node_class
class QuadraticProblem:
  """Holds the definition of the quadratic regularized OT problem.

  The quadratic loss of a single OT matrix is assumed to
  have the form given in Eq. 4 from

  http://proceedings.mlr.press/v48/peyre16.pdf

  The two geometries below parameterize matrices C and bar{C} in that equation.
  The function L (of two real values) in that equation is assumed
  to match the form given in Eq. 5. , with our notations:

  L(x, y) = lin1(x) + lin2(y) - quad1(x) * quad2(y)
  """

  def __init__(self,
               geom_1: geometry.Geometry,
               geom_2: geometry.Geometry,
               a: Optional[jnp.ndarray] = None,
               b: Optional[jnp.ndarray] = None,
               loss: Optional[Loss] = None,
               tau_a: float = 1.0,
               tau_b: float = 1.0):
    """Initializes the QuadraticProblem.

    Args:
      geom_1: the geometry.Geometry object defining the ground geometry / cost of
        of the first space.
      geom_2: the geometry.Geometry object defining the ground geometry / cost of
        of the second space.
      a: jnp.ndarray[n] representing the first marginal. If None, it will be
        uniform.
      b: jnp.ndarray[n] representing the first marginal. If None, it will be
        uniform.
      loss: a 2-tuple of 2-tuples of Callable. The first tuple is the linear
        part of the loss (see in the pydoc of the class lin1, lin2). The second
        one is the quadratic part (quad1, quad2). If None is passed, the loss
        is set as the 4 functions representing the squared euclidean loss. See
        make_square_loss and and make_kl_loss for convenient way of setting the
        loss.
      tau_a: if lower that 1.0, defines how much unbalanced the problem is on
        the first marginal.
      tau_b: if lower that 1.0, defines how much unbalanced the problem is on
        the second marginal.
    """
    self.geom_1 = geom_1
    self.geom_2 = geom_2
    self._a = a
    self._b = b
    self.tau_a = tau_a
    self.tau_b = tau_b
    self.loss = make_square_loss() if loss is None else loss

  @property
  def linear_loss(self):
    return self.loss[0]

  @property
  def quad_loss(self):
    return self.loss[1]

  @property
  def is_balanced(self):
    return self.tau_a == 1.0 and self.tau_b == 1.0

  def tree_flatten(self):
    return (
        [self.geom_1, self.geom_2, self._a, self._b],
        {'tau_a': self.tau_a, 'tau_b': self.tau_b, 'loss': self.loss})

  @classmethod
  def tree_unflatten(cls, aux_data, children):
    return cls(*children, **aux_data)

  @property
  def a(self):
    num_a = self.geom_1.shape[0]
    return jnp.ones((num_a,)) / num_a if self._a is None else self._a

  @property
  def b(self):
    num_b = self.geom_2.shape[0]
    return jnp.ones((num_b,)) / num_b if self._b is None else self._b

  def marginal_dependent_cost(self, marginal_1, marginal_2):
    r"""Calculates part of cost that depends on marginals of transport matrix.

    Uses the first term in Equation 6, Proposition 1 of
    http://proceedings.mlr.press/v48/peyre16.pdf.

    Let :math:`p` [num_a,] be the marginal of the transport matrix for samples
    from geom_x and :math:`q` [num_b,] be the marginal of the transport matrix
    for samples from geom_y. The term in the cost that depends on these
    marginals can be written as:
    marginal_dep_term = fn_x(cost_x) :math:`p \mathbb{1}_{num_b}^T`
                      + (fn_y(cost_y) :math:`q \mathbb{1}_{num_a}^T)^T`

    Args:
      marginal_1: jnp.ndarray<float>[num_a,], marginal of the transport matrix
       for samples from geom_1
      marginal_2: jnp.ndarray<float>[num_b,], marginal of the transport matrix
       for samples from geom_2

    Returns:
      jnp.ndarray, [num_a, num_b]
    """
    x_term = jnp.dot(
        self.geom_1.apply_cost(
            marginal_1[:, None], 1, self.linear_loss[0]).reshape(-1, 1),
        jnp.ones((1, marginal_2.size)))
    y_term = jnp.dot(
        self.geom_2.apply_cost(
            marginal_2[:, None], 1, self.linear_loss[1]).reshape(-1, 1),
        jnp.ones((1, marginal_1.size))).T
    return x_term + y_term