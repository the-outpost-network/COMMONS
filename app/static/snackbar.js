/* Enhance the server-rendered confirmation; without JS it stays readable inline. */
(() => {
  const snackbar = document.querySelector(".snackbar");
  if (!snackbar) return;

  // Consume the confirmation flag so a refresh doesn't announce the save again.
  const url = new URL(window.location.href);
  url.searchParams.delete("logged");
  window.history.replaceState(window.history.state, "", url);

  let timer;
  let dismissed = false;
  const pause = () => window.clearTimeout(timer);
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    pause();
    document.removeEventListener("visibilitychange", resume);
    if (snackbar.contains(document.activeElement)) {
      document.getElementById(snackbar.dataset.focusTarget)?.querySelector("summary")?.focus();
    }
    snackbar.remove();
  };
  const resume = () => {
    pause();
    if (!dismissed && !document.hidden && !snackbar.matches(":hover, :focus-within")) {
      timer = window.setTimeout(dismiss, 8000);
    }
  };

  const button = snackbar.querySelector("button");
  button.hidden = false;
  button.addEventListener("click", dismiss);
  snackbar.addEventListener("pointerenter", pause);
  snackbar.addEventListener("pointerleave", resume);
  snackbar.addEventListener("focusin", pause);
  snackbar.addEventListener("focusout", () => queueMicrotask(resume));
  document.addEventListener("visibilitychange", resume);
  // Don't bring an old confirmation back from the browser's back/forward cache.
  window.addEventListener("pagehide", dismiss, { once: true });
  snackbar.classList.add("is-active");
  resume();
})();
