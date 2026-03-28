/**
 * Audio Bridge v4: Echo gate + smaller buffers for low latency.
 *
 * Key fix: Echo suppression — when the bot is playing audio (AI response),
 * we suppress capture to prevent the bot hearing its own output and
 * causing Gemini to think the user is still speaking.
 */

(function () {
  "use strict";

  // ── Echo gate state ─────────────────────────────────────────────────
  // When true, capture is suppressed (bot is speaking)
  window.botIsSpeaking = false;
  let botSpeakingTimeout = null;

  // ── Helpers ─────────────────────────────────────────────────────────

  function bufferToBase64(buf) {
    const bytes = new Uint8Array(buf);
    let binary = "";
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }

  function base64ToBuffer(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
  }

  // ── Playback buffer (Python → JS) ──────────────────────────────────

  let playbackBuffer = new Float32Array(0);

  window.__playAudioFromPython = function (base64Audio) {
    const int16 = new Int16Array(base64ToBuffer(base64Audio));
    // Upsample 24kHz → 48kHz (2x linear interpolation)
    const upsampled = new Float32Array(int16.length * 2);
    for (let i = 0; i < int16.length; i++) {
      const s = int16[i] / 32768.0;
      upsampled[i * 2] = s;
      const next = i + 1 < int16.length ? int16[i + 1] / 32768.0 : s;
      upsampled[i * 2 + 1] = (s + next) / 2;
    }
    const newBuf = new Float32Array(playbackBuffer.length + upsampled.length);
    newBuf.set(playbackBuffer);
    newBuf.set(upsampled, playbackBuffer.length);
    playbackBuffer = newBuf;

    // Echo gate: suppress capture while playing AI audio
    window.botIsSpeaking = true;
    if (botSpeakingTimeout) clearTimeout(botSpeakingTimeout);
    // Keep gate open for 150ms after last audio chunk (covers gaps between chunks)
    botSpeakingTimeout = setTimeout(() => { window.botIsSpeaking = false; }, 150);
  };

  // ── Fake device enumeration ────────────────────────────────────────

  const originalEnumerate = navigator.mediaDevices.enumerateDevices.bind(
    navigator.mediaDevices
  );

  navigator.mediaDevices.enumerateDevices = async function () {
    const real = await originalEnumerate();
    const hasAudioIn = real.some((d) => d.kind === "audioinput");
    const hasAudioOut = real.some((d) => d.kind === "audiooutput");
    const fakeDevices = [];
    if (!hasAudioIn) {
      fakeDevices.push({
        deviceId: "pipecat-mic", kind: "audioinput",
        label: "Pipecat Virtual Microphone", groupId: "pipecat",
        toJSON() { return this; },
      });
    }
    if (!hasAudioOut) {
      fakeDevices.push({
        deviceId: "pipecat-speaker", kind: "audiooutput",
        label: "Pipecat Virtual Speaker", groupId: "pipecat",
        toJSON() { return this; },
      });
    }
    return [...real, ...fakeDevices];
  };

  setTimeout(() => {
    navigator.mediaDevices.dispatchEvent(new Event("devicechange"));
  }, 1000);

  // ── getUserMedia override (Bot Mouth) ──────────────────────────────

  const originalGUM = navigator.mediaDevices.getUserMedia.bind(
    navigator.mediaDevices
  );

  navigator.mediaDevices.getUserMedia = async function (constraints) {
    if (!constraints || !constraints.audio) {
      return originalGUM(constraints);
    }

    console.log("[AudioBridge] getUserMedia intercepted");

    try {
      const ctx = new AudioContext({ sampleRate: 48000 });

      // Smaller buffer = lower latency (1024 samples = ~21ms at 48kHz)
      const bufferSize = 1024;
      const scriptNode = ctx.createScriptProcessor(bufferSize, 0, 1);

      scriptNode.onaudioprocess = function (e) {
        const output = e.outputBuffer.getChannelData(0);
        if (playbackBuffer.length >= output.length) {
          output.set(playbackBuffer.subarray(0, output.length));
          playbackBuffer = playbackBuffer.slice(output.length);
        } else {
          output.set(playbackBuffer);
          for (let i = playbackBuffer.length; i < output.length; i++) output[i] = 0;
          playbackBuffer = new Float32Array(0);
        }
      };

      const dest = ctx.createMediaStreamDestination();
      scriptNode.connect(dest);

      window.__pipecatPlayer = scriptNode;
      window.__pipecatAudioCtx = ctx;

      const audioStream = dest.stream;

      if (constraints.video) {
        try {
          const videoStream = await originalGUM({ video: constraints.video });
          return new MediaStream([
            ...audioStream.getAudioTracks(),
            ...videoStream.getVideoTracks(),
          ]);
        } catch (e) { return audioStream; }
      }
      return audioStream;
    } catch (err) {
      console.error("[AudioBridge] Failed:", err.message);
      try {
        const ctx = new AudioContext({ sampleRate: 48000 });
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        gain.gain.value = 0;
        osc.connect(gain);
        const dest = ctx.createMediaStreamDestination();
        gain.connect(dest);
        osc.start();
        return dest.stream;
      } catch (e2) {
        return originalGUM(constraints);
      }
    }
  };

  // ── Audio capture (Bot Ears) — single instance guard ────────────────

  window.__pipecatCaptureActive = false;

  // Capture audio from a MediaStream (called once, guarded)
  window.__pipecatStartCapture = function (stream) {
    if (window.__pipecatCaptureActive) return false;
    if (!stream || stream.getAudioTracks().length === 0) return false;
    window.__pipecatCaptureActive = true;

    const ctx = new AudioContext({ sampleRate: 48000 });
    const source = ctx.createMediaStreamSource(stream);
    const captureNode = ctx.createScriptProcessor(1024, 1, 1);
    let residual = new Float32Array(0);

    captureNode.onaudioprocess = function (e) {
      const input = e.inputBuffer.getChannelData(0);

      // During bot speech: check if user is interrupting.
      // If user speaks loudly → clear playback buffer (instant silence)
      // and send audio at full volume so Deepgram detects the interruption.
      // If no user speech → send silence (Deepgram handles VAD server-side).
      let processedInput = input;
      if (window.botIsSpeaking) {
        let maxAbs = 0;
        for (let i = 0; i < input.length; i++) {
          maxAbs = Math.max(maxAbs, Math.abs(input[i]));
        }
        if (maxAbs > 0.02) {
          // User is interrupting — clear bot audio, send at full volume
          playbackBuffer = new Float32Array(0);
          window.botIsSpeaking = false;
        } else {
          // Silence during bot speech — full mute to prevent echo
          processedInput = new Float32Array(input.length);
        }
      }

      const combined = new Float32Array(residual.length + processedInput.length);
      combined.set(residual);
      combined.set(processedInput, residual.length);
      const decimatedLen = Math.floor(combined.length / 3);
      if (decimatedLen === 0) { residual = combined; return; }
      const int16 = new Int16Array(decimatedLen);
      for (let i = 0; i < decimatedLen; i++) {
        const s = Math.max(-1, Math.min(1, combined[i * 3]));
        int16[i] = s < 0 ? s * 32768 : s * 32767;
      }
      residual = combined.slice(decimatedLen * 3);
      if (typeof sendAudioToPython === "function") {
        sendAudioToPython(bufferToBase64(int16.buffer));
      }
      e.outputBuffer.getChannelData(0).fill(0);
    };

    source.connect(captureNode);
    captureNode.connect(ctx.destination);
    console.log("[AudioBridge] Single capture active");
    return true;
  };

  // ── RTCPeerConnection hook ─────────────────────────────────────────

  window.__pipecatPCs = window.__pipecatPCs || [];
  const OrigRTC = window.RTCPeerConnection;

  window.RTCPeerConnection = function (...args) {
    const pc = new OrigRTC(...args);
    window.__pipecatPCs.push(pc);

    pc.addEventListener("track", async (event) => {
      if (event.track.kind !== "audio") return;
      console.log("[AudioBridge] Remote audio track received");
      window.__pipecatStartCapture(new MediaStream([event.track]));
    });

    return pc;
  };

  Object.setPrototypeOf(window.RTCPeerConnection, OrigRTC);
  Object.setPrototypeOf(window.RTCPeerConnection.prototype, OrigRTC.prototype);

  console.log("[AudioBridge v4] Initialized (echo gate + low latency)");
})();
