// Géré par Ansible (rôle status).
// Une page d'état qui reste verte alors que plus rien ne la met à jour ment :
// ce script affiche l'âge du relevé et un bandeau rouge s'il est périmé.
"use strict";
(function () {
  var root = document.documentElement;
  var generated = Number(root.getAttribute("data-generated"));
  var staleAfter = Number(root.getAttribute("data-stale-after"));
  var age = document.getElementById("age");
  var banner = document.getElementById("stale");
  if (!generated) { return; }

  function human(seconds) {
    if (seconds < 90) { return seconds + " s"; }
    var minutes = Math.round(seconds / 60);
    if (minutes < 90) { return minutes + " min"; }
    var hours = Math.floor(minutes / 60);
    if (hours < 48) { return hours + " h " + ("0" + (minutes % 60)).slice(-2) + " min"; }
    return Math.floor(hours / 24) + " j " + (hours % 24) + " h";
  }

  function update() {
    var seconds = Math.max(0, Math.round(Date.now() / 1000 - generated));
    if (age) { age.textContent = "il y a " + human(seconds); }
    if (banner) { banner.hidden = seconds <= staleAfter; }
  }

  update();
  setInterval(update, 15000);
})();
