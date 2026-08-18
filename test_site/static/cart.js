(function () {
  "use strict";

  const CART_KEY = "atl_cart";

  function getCart() {
    try {
      return JSON.parse(localStorage.getItem(CART_KEY)) || [];
    } catch (e) {
      return [];
    }
  }

  function saveCart(cart) {
    localStorage.setItem(CART_KEY, JSON.stringify(cart));
    updateCartBadge();
  }

  function addToCart(id, name, price) {
    const cart = getCart();
    const existing = cart.find((item) => item.id === id);
    if (existing) {
      existing.qty += 1;
    } else {
      cart.push({ id, name, price, qty: 1 });
    }
    saveCart(cart);
    if (window.ATL) window.ATL.track("add_to_cart", { id, name, price });
  }

  function clearCart() {
    saveCart([]);
  }

  function updateCartBadge() {
    const el = document.getElementById("cart-count");
    if (!el) return;
    const count = getCart().reduce((sum, item) => sum + item.qty, 0);
    el.textContent = String(count);
  }

  window.ATLCart = { getCart, saveCart, addToCart, clearCart, updateCartBadge };
  document.addEventListener("DOMContentLoaded", updateCartBadge);
})();
