document.addEventListener("DOMContentLoaded", () => {
  const pad = (part) => String(part).padStart(2, "0");
  document.querySelectorAll("[data-local-datetime]").forEach((input) => {
    const stored = input.dataset.utcValue;
    if (stored) {
      const date = new Date(`${stored}${stored.endsWith("Z") ? "" : "Z"}`);
      input.value = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }
    input.form?.addEventListener("submit", () => {
      const hidden = input.form.querySelector(`input[type="hidden"][name="${input.name.replace("_local", "")}"]`);
      hidden.value = input.value ? new Date(input.value).toISOString() : "";
      input.removeAttribute("name");
    }, { once: true });
  });

  document.querySelectorAll("[data-local-time]").forEach((element) => {
    const raw = element.dataset.localTime;
    const date = new Date(`${raw}${raw.endsWith("Z") ? "" : "Z"}`);
    if (Number.isNaN(date.getTime())) return;
    const options = element.hasAttribute("data-date-only")
      ? { dateStyle: "medium" }
      : { dateStyle: "medium", timeStyle: "short" };
    element.textContent = new Intl.DateTimeFormat(undefined, options).format(date);
  });

  const navToggle = document.querySelector("[data-nav-toggle]");
  const topnav = document.querySelector("[data-topnav]");

  navToggle?.addEventListener("click", () => {
    const expanded = navToggle.getAttribute("aria-expanded") === "true";
    navToggle.setAttribute("aria-expanded", String(!expanded));
    topnav?.classList.toggle("is-open", !expanded);
  });

  document.querySelectorAll("[data-confirm]").forEach((element) => {
    element.addEventListener("click", (event) => {
      if (!window.confirm(element.dataset.confirm)) event.preventDefault();
    });
  });

  const form = document.querySelector("[data-exam-form]");
  if (!form) return;

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
  const timer = document.querySelector("[data-exam-timer]");
  const timerValue = document.querySelector("[data-timer-value]");
  const saveStatus = document.querySelector("[data-save-status]");
  const gate = document.querySelector("[data-proctoring-gate]");
  const gateStatus = document.querySelector("[data-proctoring-status]");
  const startButton = document.querySelector("[data-proctoring-start]");
  const cameraVideo = document.querySelector("[data-proctoring-video]");
  let deadline = Number(document.querySelector('meta[name="exam-deadline"]')?.content) * 1000;
  let started = form.dataset.started === "true";
  const queueByQuestion = new Map();
  const lastViolationByType = new Map();
  let lastViolationAt = 0;
  let violationQueue = Promise.resolve();
  let cameraStream = null;
  let faceDetector = null;
  let faceMonitor = null;
  let faceCheckRunning = false;
  let missingFaceChecks = 0;
  let faceMissingReported = false;
  let timerInterval = null;
  let submitting = false;

  function updateProgress() {
    const questions = [...form.querySelectorAll("[data-question-id]")];
    const answered = new Set(
      questions.filter((input) => input.checked).map((input) => input.dataset.questionId),
    );
    document.querySelector("[data-answered-count]").textContent = String(answered.size);
    document.querySelector("[data-progress-bar]").style.width =
      `${questions.length ? (answered.size / new Set(questions.map((input) => input.dataset.questionId)).size) * 100 : 0}%`;
    document.querySelectorAll("[data-question-nav]").forEach((link) => {
      link.classList.toggle("is-answered", answered.has(link.dataset.questionNav));
    });
    form.querySelectorAll(".exam-option").forEach((label) => {
      label.classList.toggle("is-selected", label.querySelector("input").checked);
    });
  }

  async function saveAnswer(input) {
    const questionId = input.dataset.questionId;
    const optionId = input.checked ? Number(input.value) : null;
    const previous = queueByQuestion.get(questionId) || Promise.resolve();
    const queued = previous.catch(() => undefined).then(async () => {
      if (submitting) return;
      saveStatus.textContent = "Saving…";
      timer.classList.remove("save-error");
      const response = await fetch(form.dataset.autosaveUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ question_id: Number(questionId), option_id: optionId }),
      });
      const payload = await response.json();
      if (payload.redirect) {
        window.location.assign(payload.redirect);
        return;
      }
      if (!response.ok) throw new Error(payload.error || "Your answer could not be saved.");
      saveStatus.textContent = "All changes saved";
    });
    queueByQuestion.set(questionId, queued);
    queued.catch((error) => {
      saveStatus.textContent = error.message || "Connection issue — try selecting again.";
      timer.classList.add("save-error");
    });
    return queued;
  }

  form.querySelectorAll("[data-question-id]").forEach((input) => {
    input.addEventListener("change", () => {
      updateProgress();
      saveAnswer(input).catch(() => undefined);
    });
  });

  form.addEventListener("submit", (event) => {
    if (!started) {
      event.preventDefault();
      return;
    }
    if (submitting) {
      event.preventDefault();
      return;
    }
    submitting = true;
    form.querySelectorAll("button[type=submit]").forEach((button) => {
      button.disabled = true;
      button.textContent = "Submitting…";
    });
  });

  updateProgress();

  let autoSubmitStarted = false;
  async function tick() {
    if (!deadline) return;
    const seconds = Math.max(0, Math.floor((deadline - Date.now()) / 1000));
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    timerValue.textContent = `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
    if (seconds <= 60) timer.classList.add("timer-warning");
    if (seconds > 0 || autoSubmitStarted) return;
    autoSubmitStarted = true;
    saveStatus.textContent = "Time is up — submitting your saved answers…";
    await Promise.allSettled([...queueByQuestion.values()]);
    form.requestSubmit();
  }

  function startTimer() {
    tick();
    if (!timerInterval) timerInterval = window.setInterval(tick, 1000);
  }

  function showGate(message) {
    form.hidden = true;
    gate.hidden = false;
    gateStatus.textContent = message;
    startButton.textContent = started
      ? "Resume exam checks"
      : "Enable camera and enter fullscreen";
  }

  async function loadFaceDetector() {
    if (faceDetector) return;
    gateStatus.textContent = "Loading the local face detector…";
    const { FaceDetector, FilesetResolver } = await import(
      "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.3/vision_bundle.mjs"
    );
    const vision = await FilesetResolver.forVisionTasks(
      "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.3/wasm",
    );
    faceDetector = await FaceDetector.createFromOptions(vision, {
      baseOptions: {
        modelAssetPath: "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
        delegate: "CPU",
      },
      runningMode: "VIDEO",
      minDetectionConfidence: 0.6,
    });
  }

  function detectFace() {
    return cameraVideo.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
      && faceDetector.detectForVideo(cameraVideo, performance.now()).detections.length > 0;
  }

  async function waitForFace() {
    const stopAt = Date.now() + 45000;
    while (Date.now() < stopAt) {
      if (!document.fullscreenElement) {
        throw new Error("Fullscreen was closed. Click the button to try again.");
      }
      if (detectFace()) return;
      gateStatus.textContent = "Center your face in the camera preview to continue.";
      await new Promise((resolve) => window.setTimeout(resolve, 400));
    }
    throw new Error("No face was detected. Check the camera view and try again.");
  }

  function lockExam(message) {
    if (!started) return;
    showGate(message);
  }

  function reportViolation(eventType) {
    const now = Date.now();
    if (
      now - lastViolationAt < 1000
      || now - (lastViolationByType.get(eventType) || 0) < 2000
    ) {
      return Promise.resolve();
    }
    lastViolationAt = now;
    lastViolationByType.set(eventType, now);
    violationQueue = violationQueue.catch(() => undefined).then(async () => {
      await Promise.allSettled([...queueByQuestion.values()]);
      const response = await fetch(gate.dataset.violationsUrl, {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ event_type: eventType }),
      });
      const payload = await response.json();
      if (payload.redirect) {
        window.location.assign(payload.redirect);
        return;
      }
      if (!response.ok) throw new Error(payload.error || "The proctoring event could not be recorded.");
      saveStatus.textContent = `${payload.violations} of 3 proctoring violations`;
      if (payload.status === "submitted") window.location.assign(form.dataset.resultUrl);
    });
    return violationQueue;
  }

  async function monitorFace() {
    if (!started || document.hidden || faceCheckRunning || !faceDetector || !cameraStream) return;
    faceCheckRunning = true;
    try {
      if (detectFace()) {
        missingFaceChecks = 0;
        faceMissingReported = false;
      } else {
        missingFaceChecks += 1;
        if (missingFaceChecks >= 3 && !faceMissingReported) {
          faceMissingReported = true;
          lockExam("Face not visible. Center your face and resume the exam checks.");
          await reportViolation("face_missing");
        }
      }
    } catch (error) {
      lockExam("The local face check failed. Please retry the camera check.");
      gateStatus.textContent = error.message || "The local face check failed.";
    } finally {
      faceCheckRunning = false;
    }
  }

  function startFaceMonitoring() {
    if (faceMonitor) return;
    faceMonitor = window.setInterval(monitorFace, 1000);
  }

  async function startProctoredAttempt() {
    startButton.disabled = true;
    gateStatus.textContent = "Requesting camera and fullscreen access…";
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("Camera access requires a supported browser and a secure connection.");
      }

      const fullscreenPromise = document.fullscreenElement
        ? Promise.resolve()
        : document.documentElement.requestFullscreen
          ? document.documentElement.requestFullscreen()
          : Promise.reject(new Error("Fullscreen is not supported in this browser."));
      const cameraPromise = cameraStream
        && cameraStream.getVideoTracks().some((track) => track.readyState === "live")
        ? Promise.resolve(cameraStream)
        : navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" }, audio: false });
      const [cameraResult, fullscreenResult] = await Promise.allSettled([
        cameraPromise,
        fullscreenPromise,
      ]);
      if (cameraResult.status === "fulfilled") {
        cameraStream = cameraResult.value;
        cameraVideo.srcObject = cameraStream;
      }
      if (cameraResult.status === "rejected") throw cameraResult.reason;
      if (fullscreenResult.status === "rejected") throw fullscreenResult.reason;

      await cameraVideo.play();
      await loadFaceDetector();
      await waitForFace();
      gateStatus.textContent = "Checks passed. Starting your examination…";
      const response = await fetch(gate.dataset.beginUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({}),
      });
      const payload = await response.json();
      if (payload.redirect) {
        window.location.assign(payload.redirect);
        return;
      }
      if (!response.ok) throw new Error(payload.error || "The examination could not be started.");
      deadline = Number(payload.deadline) * 1000;
      started = true;
      form.dataset.started = "true";
      gate.hidden = true;
      form.hidden = false;
      startTimer();
      startFaceMonitoring();
    } catch (error) {
      gateStatus.textContent = error.message || "Camera and fullscreen checks could not be completed.";
    } finally {
      startButton.disabled = false;
    }
  }

  startButton.addEventListener("click", startProctoredAttempt);

  document.addEventListener("visibilitychange", () => {
    if (!started) return;
    if (document.hidden) {
      lockExam("Exam paused. Return to this tab and resume the fullscreen check.");
      reportViolation("tab_switch").catch((error) => {
        gateStatus.textContent = error.message || "The tab-switch event could not be recorded.";
      });
    } else {
      lockExam("Return to fullscreen and click Resume exam checks.");
    }
  });

  document.addEventListener("fullscreenchange", () => {
    if (started && !document.hidden && !document.fullscreenElement) {
      lockExam("Fullscreen was closed. Click Resume exam checks.");
      reportViolation("fullscreen_exit").catch((error) => {
        gateStatus.textContent = error.message || "The fullscreen event could not be recorded.";
      });
    }
  });

  function blockClipboard(event) {
    if (!started || form.hidden) return;
    event.preventDefault();
    reportViolation("clipboard_attempt").catch((error) => {
      lockExam(error.message || "The clipboard event could not be recorded.");
    });
  }

  ["copy", "cut", "paste"].forEach((eventName) => {
    document.addEventListener(eventName, blockClipboard, true);
  });
  document.addEventListener("keydown", (event) => {
    if (
      started
      && !form.hidden
      && (event.ctrlKey || event.metaKey)
      && ["c", "x", "v"].includes(event.key.toLowerCase())
    ) {
      event.preventDefault();
      reportViolation("clipboard_attempt").catch((error) => {
        lockExam(error.message || "The clipboard event could not be recorded.");
      });
    }
  }, true);

  window.addEventListener("beforeunload", () => {
    cameraStream?.getTracks().forEach((track) => track.stop());
  });

  if (deadline) startTimer();
});
