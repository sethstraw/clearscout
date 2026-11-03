// This app's CSP has no 'unsafe-inline', so the page's own inline call that
// starts the JSON viewers never runs. Start them from an external file instead.
$(function () {
  initJsonViewers();
});
