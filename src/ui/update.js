// ------------------------------------------------------- the offer of an update
// Python has already decided whether there's anything to say — it knows the
// version on disk and the one in the repository. An empty object means say
// nothing, which is also what a copy with no internet and a clone with a .git
// folder both get.
const UPDATE = __UPDATE__;

// However far behind this copy is, the offer reads the same. Being eleven
// versions back is no more alarming than being one, and saying so would only
// invite someone to wonder what they missed.
if (UPDATE.show) {
  $('updLead').textContent = `Version ${UPDATE.version} is available`;
  $('updText').textContent =
    'Update to the latest version? It will only take a minute or two.';
  $('updateBar').hidden = false;
}

// "Not now" is only good for this window. The offer comes back on the next
// launch, because putting an update off is rarely the same as not wanting it.
$('updSkip').onclick = () => { $('updateBar').hidden = true; };

$('updGo').onclick = async () => {
  // Python is busy downloading for most of this, so it can't report progress
  // back. Saying how long it takes and what not to do is the whole of the
  // reassurance available.
  $('updGo').disabled = true;
  $('updSkip').disabled = true;
  $('updGo').textContent = 'Updating…';
  say('updMsg', 'Downloading and installing. This takes a minute or two — '
    + 'leave this window open until it finishes.');
  const res = await window.pyUpdateNow();
  if (res.error) {
    $('updGo').disabled = false;
    $('updSkip').disabled = false;
    $('updGo').textContent = 'Update now';
    say('updMsg', res.error, 'bad');
    return;
  }
  $('updateBar').hidden = true;
  $('updateDoneText').textContent =
    [res.message].concat(res.notes || []).join(' ');
  $('updateDone').hidden = false;
  if (res.restart) {
    $('updateDoneClose').textContent = 'Restart now';
    // Closing on its own is the point: the new version is on disk, and the only
    // thing standing between the person and it is this window. A note means
    // there's something to read first, so that one waits to be dismissed.
    if (!(res.notes || []).length) setTimeout(finishUpdate, 2500);
  }
  $('updateDoneClose').focus();
};

// Nothing else in this window is worth doing now, so the only button closes it.
// Submitting rather than cancelling is what gets the terminal to explain why it
// stopped instead of reporting that the run was called off. Guarded because the
// timer above and an impatient click both end up here.
function finishUpdate() {
  if (finishUpdate.already) return;
  finishUpdate.already = true;
  window.pySubmit({action: 'updated'});
}

$('updateDoneClose').onclick = finishUpdate;
