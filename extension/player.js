// =============================================================================
// player.js, which is the dashboard tab only and plays no audio.
// =============================================================================
// player.html is the visible dashboard UI. Every bit of audio playback happens
// in the offscreen document in offscreen.js, and this tab must never play audio
// or it would double-play and run into the tab autoplay block. It also must
// never leave a message channel open, which is what used to produce "message
// channel closed before a response was received".
//
// background.js tags the audio-control messages with target:"offscreen" and I
// just ignore those here.
// =============================================================================

// Announce this tab to the background so it can be focused/reused.
chrome.runtime.sendMessage({ action: "playerReady" }).catch(() => {});

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  // Ignore everything addressed to the offscreen audio sink.
  if (request && request.target === "offscreen") return false;

  // The dashboard runs off dashboard.js, which polls status and console, and
  // off chrome.storage updates, rather than runtime messages. Returning false
  // here releases the channel straight away so it can never close mid-pending.
  return false;
});
