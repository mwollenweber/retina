/* Google Analytics 4 loader. Loaded on the map page only; submit.html
   deliberately omits it so people reporting cameras are not tracked.
   Does nothing unless RETINA_CONFIG.GA_MEASUREMENT_ID is set. */
(function () {
  const id = (window.RETINA_CONFIG || {}).GA_MEASUREMENT_ID;
  if (!id) return;

  window.dataLayer = window.dataLayer || [];
  window.gtag = function () {
    window.dataLayer.push(arguments);
  };
  window.gtag("js", new Date());
  // Disable Google signals and ad personalization so visits are not joined
  // to signed-in Google accounts or used for ads.
  window.gtag("config", id, {
    allow_google_signals: false,
    allow_ad_personalization_signals: false,
  });

  const s = document.createElement("script");
  s.async = true;
  s.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(id);
  document.head.appendChild(s);
})();
