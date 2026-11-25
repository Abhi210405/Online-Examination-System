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

  document.querySelectorAll("[data-role-select]").forEach((select) => {
    const form = select.form;
    const fields = form?.querySelector("[data-student-fields]");
    if (!fields) return;
    const update = () => {
      const visible = select.value === "student";
      fields.hidden = !visible;
      fields.querySelectorAll("input").forEach((input) => {
        input.required = visible;
      });
    };
    select.addEventListener("change", update);
    update();
  });

  document.querySelectorAll("[data-question-type]").forEach((select) => {
    const fields = select.form?.querySelector("[data-mcq-fields]");
    if (!fields) return;
    const update = () => {
      const visible = select.value === "mcq";
      fields.hidden = !visible;
      fields.querySelectorAll("input, select").forEach((input) => {
        input.required = visible;
      });
    };
    select.addEventListener("change", update);
    update();
  });

  document.querySelectorAll("[data-enhanced-table]").forEach((table) => {
    const rows = [...table.querySelectorAll("tbody tr")].filter((row) => row.cells.length > 1);
    if (rows.length === 0) return;
    const wrapper = table.closest(".table-wrap");
    const controls = document.createElement("div");
    controls.className = "table-controls";
    const searchLabel = document.createElement("label");
    searchLabel.className = "table-search";
    searchLabel.textContent = "Search";
    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = "Search this table";
    search.setAttribute("aria-label", "Search table rows");
    searchLabel.append(search);
    const pager = document.createElement("div");
    pager.className = "table-pager";
    const previous = document.createElement("button");
    previous.type = "button";
    previous.className = "button button-outline button-sm";
    previous.textContent = "Previous";
    const status = document.createElement("span");
    status.setAttribute("aria-live", "polite");
    const next = document.createElement("button");
    next.type = "button";
    next.className = "button button-outline button-sm";
    next.textContent = "Next";
    pager.append(previous, status, next);
    controls.append(searchLabel, pager);
    wrapper?.parentNode.insertBefore(controls, wrapper);
    let page = 0;
    const pageSize = 10;
    const paint = () => {
      const query = search.value.trim().toLocaleLowerCase();
      const filtered = rows.filter((row) => row.textContent.toLocaleLowerCase().includes(query));
      const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
      page = Math.min(page, pageCount - 1);
      rows.forEach((row) => {
        row.hidden = !filtered.includes(row);
      });
      filtered.slice(page * pageSize, (page + 1) * pageSize).forEach((row) => {
        row.hidden = false;
      });
      status.textContent = filtered.length
        ? `${page * pageSize + 1}–${Math.min((page + 1) * pageSize, filtered.length)} of ${filtered.length}`
        : "No matching rows";
      previous.disabled = page === 0;
      next.disabled = page >= pageCount - 1;
    };
    search.addEventListener("input", () => {
      page = 0;
      paint();
    });
    previous.addEventListener("click", () => {
      page -= 1;
      paint();
    });
    next.addEventListener("click", () => {
      page += 1;
      paint();
    });
    paint();
  });

  document.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
    let points;
    try {
      points = JSON.parse(canvas.dataset.chartData || "[]");
    } catch (error) {
      console.error("Could not read chart data.", error);
      return;
    }
    if (!Array.isArray(points) || points.length === 0) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const tooltip = canvas.parentElement.querySelector("[data-chart-tooltip]");
    const render = () => {
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(280, canvas.clientWidth);
      const height = 220;
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      const margin = { top: 14, right: 12, bottom: 42, left: 36 };
      const chartWidth = width - margin.left - margin.right;
      const chartHeight = height - margin.top - margin.bottom;
      const maximum = Math.max(1, ...points.map((point) => Number(point.value) || 0));
      const ceiling = Math.ceil(maximum / 10) * 10 || 10;
      const chartHits = [];
      context.font = "10px DM Sans, Arial";
      context.textAlign = "right";
      context.fillStyle = "#929bad";
      for (let tick = 0; tick <= 4; tick += 1) {
        const y = margin.top + chartHeight - (chartHeight * tick) / 4;
        context.strokeStyle = "#edf0f5";
        context.beginPath();
        context.moveTo(margin.left, y);
        context.lineTo(width - margin.right, y);
        context.stroke();
        context.fillText(String(Math.round((ceiling * tick) / 4)), margin.left - 7, y + 3);
      }
      const slotWidth = chartWidth / points.length;
      if (canvas.dataset.chart === "line") {
        context.strokeStyle = "#596ee5";
        context.lineWidth = 2;
        context.beginPath();
      }
      points.forEach((point, index) => {
        const value = Math.max(0, Number(point.value) || 0);
        const barHeight = (value / ceiling) * chartHeight;
        const centerX = margin.left + slotWidth * (index + 0.5);
        const y = margin.top + chartHeight - barHeight;
        if (canvas.dataset.chart === "line") {
          if (index === 0) context.moveTo(centerX, y);
          else context.lineTo(centerX, y);
          context.fillStyle = "#596ee5";
          context.beginPath();
          context.arc(centerX, y, 3.5, 0, Math.PI * 2);
          context.fill();
          chartHits.push({ x: centerX, y, width: slotWidth, label: point.label, value: point.value });
          context.strokeStyle = "#596ee5";
          context.beginPath();
          if (index > 0) {
            const prior = points[index - 1];
            const priorValue = Math.max(0, Number(prior.value) || 0);
            const priorX = margin.left + slotWidth * (index - 0.5);
            const priorY = margin.top + chartHeight - (priorValue / ceiling) * chartHeight;
            context.moveTo(priorX, priorY);
            context.lineTo(centerX, y);
            context.stroke();
          }
        } else {
          const barWidth = Math.min(42, slotWidth * 0.58);
          context.fillStyle = index % 2 ? "#7659e9" : "#596ee5";
          context.beginPath();
          context.fillRect(centerX - barWidth / 2, y, barWidth, barHeight);
          context.fill();
          chartHits.push({ x: centerX - barWidth / 2, y, width: barWidth, height: barHeight, label: point.label, value: point.value });
        }
        context.fillStyle = "#7e8799";
        context.textAlign = "center";
        context.fillText(String(point.label).slice(0, 14), centerX, height - 19);
        context.fillStyle = "#49546b";
        context.fillText(String(point.value), centerX, Math.max(margin.top + 10, y - 6));
      });
      canvas.dataset.chartHits = JSON.stringify(chartHits);
    };
    canvas.addEventListener("mousemove", (event) => {
      if (!tooltip) return;
      const bounds = canvas.getBoundingClientRect();
      const x = event.clientX - bounds.left;
      const y = event.clientY - bounds.top;
      let hovered;
      try {
        const hits = JSON.parse(canvas.dataset.chartHits || "[]");
        if (canvas.dataset.chart === "line") {
          hovered = hits.find((point) =>
            Math.abs(x - point.x) <= point.width / 2 && Math.abs(y - point.y) <= 14);
        } else {
          hovered = hits.find((bar) =>
            x >= bar.x && x <= bar.x + bar.width && y >= bar.y && y <= bar.y + bar.height);
        }
      } catch {
        return;
      }
      tooltip.textContent = hovered ? `${hovered.label}: ${hovered.value}` : "";
    });
    render();
    window.addEventListener("resize", render);
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
  const questionPanels = [...form.querySelectorAll("[data-question-panel]")];
  const saveTimers = new Map();
  let activeQuestion = 0;

  function updateProgress() {
    const answered = new Set();
    const marked = new Set();
    questionPanels.forEach((panel) => {
      const questionId = panel.dataset.questionPanel;
      const response = panel.querySelector("[data-response-text]");
      const hasAnswer = response
        ? Boolean(response.value.trim())
        : Boolean(panel.querySelector("[data-question-input]:checked"));
      if (hasAnswer) answered.add(questionId);
      const reviewButton = panel.querySelector("[data-review-toggle]");
      if (reviewButton?.getAttribute("aria-pressed") === "true") marked.add(questionId);
    });
    document.querySelector("[data-answered-count]").textContent = String(answered.size);
    document.querySelector("[data-progress-bar]").style.width =
      `${questionPanels.length ? (answered.size / questionPanels.length) * 100 : 0}%`;
    document.querySelectorAll("[data-question-nav]").forEach((link) => {
      link.classList.toggle("is-answered", answered.has(link.dataset.questionNav));
      link.classList.toggle("is-marked", marked.has(link.dataset.questionNav));
      link.classList.toggle("is-current", Number(link.dataset.questionIndex) === activeQuestion);
    });
    form.querySelectorAll(".exam-option").forEach((label) => {
      label.classList.toggle("is-selected", label.querySelector("input").checked);
    });
  }

  function showQuestion(index, focus = false) {
    activeQuestion = Math.max(0, Math.min(index, questionPanels.length - 1));
    questionPanels.forEach((panel, panelIndex) => {
      panel.hidden = panelIndex !== activeQuestion;
    });
    updateProgress();
    if (focus) {
      questionPanels[activeQuestion]?.scrollIntoView({ behavior: "smooth", block: "start" });
      questionPanels[activeQuestion]?.querySelector("[data-question-input]")?.focus({ preventScroll: true });
    }
  }

  async function saveAnswer(questionId) {
    const panel = form.querySelector(`[data-question-panel="${questionId}"]`);
    const selected = panel.querySelector("[data-question-input]:checked");
    const responseText = panel.querySelector("[data-response-text]");
    const review = panel.querySelector("[data-review-toggle]")?.getAttribute("aria-pressed") === "true";
    const previous = queueByQuestion.get(questionId) || Promise.resolve();
    const queued = previous.catch(() => undefined).then(async () => {
      saveStatus.textContent = "Saving…";
      timer.classList.remove("save-error");
      const response = await fetch(form.dataset.autosaveUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          question_id: Number(questionId),
          option_id: selected ? Number(selected.value) : null,
          response_text: responseText ? responseText.value : null,
          is_marked_for_review: review,
        }),
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

  form.querySelectorAll("[data-question-input]").forEach((input) => {
    const saveAndRefresh = () => {
      updateProgress();
      saveAnswer(input.dataset.questionId).catch(() => undefined);
    };
    input.addEventListener(input.matches("textarea") ? "input" : "change", () => {
      if (input.matches("textarea")) {
        updateProgress();
        window.clearTimeout(saveTimers.get(input.dataset.questionId));
        saveTimers.set(
          input.dataset.questionId,
          window.setTimeout(
            () => saveAnswer(input.dataset.questionId).catch(() => undefined),
            450,
          ),
        );
      } else {
        saveAndRefresh();
      }
    });
    if (input.matches("textarea")) {
      input.addEventListener("blur", () => {
        window.clearTimeout(saveTimers.get(input.dataset.questionId));
        saveAndRefresh();
      });
    }
  });

  form.querySelectorAll("[data-review-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const marked = button.getAttribute("aria-pressed") !== "true";
      button.setAttribute("aria-pressed", String(marked));
      button.classList.toggle("is-marked", marked);
      button.querySelector("span").textContent = marked ? "Marked for review" : "Mark for review";
      updateProgress();
      saveAnswer(button.dataset.reviewToggle).catch(() => undefined);
    });
  });

  questionPanels.forEach((panel, index) => {
    panel.querySelector("[data-question-prev]")?.addEventListener("click", () => showQuestion(index - 1, true));
    panel.querySelector("[data-question-next]")?.addEventListener("click", () => showQuestion(index + 1, true));
  });
  document.querySelectorAll("[data-question-nav]").forEach((button, index) => {
    button.dataset.questionIndex = String(index);
    button.addEventListener("click", () => showQuestion(index, true));
  });
  showQuestion(0);

  form.addEventListener("submit", (event) => {
    if (!started) {
      event.preventDefault();
      return;
    }
    if (submitting) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    submitting = true;
    form.querySelectorAll("button[type=submit]").forEach((button) => {
      button.disabled = true;
      button.textContent = "Submitting…";
    });
    saveStatus.textContent = "Saving final changes…";
    Promise.allSettled([...queueByQuestion.values()]).then(() => form.submit());
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
