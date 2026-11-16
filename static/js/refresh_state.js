/* Preserve the active dashboard view and classroom context across a page reload. */
(function (global, document) {
  "use strict";
  if (!document.querySelector || !document.querySelector(".view")) return;

  var parts = (global.location.pathname || "/").split("/").filter(Boolean);
  var base = "/" + parts.slice(0, 2).join("/");
  var key = "tass_refresh_state:" + base;
  var restoring = false;

  function activeViewId() {
    var active = document.querySelector(".view.active");
    return active ? active.id.replace(/^view-/, "") : "";
  }

  function activePane() {
    var view = document.querySelector(".view.active") || document;
    return view.querySelector(".tab-pane.active, .dtab-pane.active, .gp-pane.active, .struct-pane.active, [role='tabpanel'].active");
  }

  function snapshot() {
    var pane = activePane();
    var classroomId = global.currentVCId || global.currentSVCId ||
      (global.__saClassroom && global.__saClassroom.virtualClassroom && global.__saClassroom.virtualClassroom.id) || null;
    var subjectId = global.currentSVCId || null;
    var tabButton = document.querySelector(".view.active .tab-btn.active, .view.active .dtab-btn.active, .view.active .gp-tab.active, .view.active .struct-tab.active");
    var customTab = document.querySelector("#vcViewerTabs .tab-btn.active[data-tab]");
    var teacherDetailId = global.__TASS_OPEN_TEACHER_ID || null;
    if (!teacherDetailId && typeof global.__TASS_GET_TEACHER_DETAIL_ID === "function") {
      try { teacherDetailId = global.__TASS_GET_TEACHER_DETAIL_ID(); } catch (e) {}
    }
    return {
      view: activeViewId(),
      pane: pane ? pane.id : "",
      tab: customTab ? customTab.getAttribute("data-tab") : (tabButton ? tabButton.getAttribute("data-tab") || "" : ""),
      classroomId: classroomId == null ? "" : String(classroomId),
      subjectId: subjectId == null ? "" : String(subjectId),
      teacherDetailId: teacherDetailId == null ? "" : String(teacherDetailId),
      savedAt: Date.now()
    };
  }

  function save() {
    if (restoring) return;
    var state = snapshot();
    if (!state.view) return;
    try { global.sessionStorage.setItem(key, JSON.stringify(state)); } catch (e) {}
  }

  function read() {
    try {
      var state = JSON.parse(global.sessionStorage.getItem(key) || "null");
      return state && state.view ? state : null;
    } catch (e) { return null; }
  }

  function tabIdFromPane(pane) {
    if (!pane) return "";
    return pane.id.replace(/^(tab-|dtab-|gp-|struct-)/, "");
  }

  function buttonForTab(id) {
    var buttons = document.querySelectorAll(".view.active .tab-btn, .view.active .dtab-btn, .view.active .gp-tab, .view.active .struct-tab");
    for (var i = 0; i < buttons.length; i++) {
      var button = buttons[i];
      var click = button.getAttribute("onclick") || "";
      if (button.getAttribute("data-tab") === id || click.indexOf("'" + id + "'") !== -1 || click.indexOf('"' + id + '"') !== -1) return button;
    }
    return null;
  }

  function restorePane(state) {
    if (state.tab && typeof global.switchVCViewerTab === "function" && document.getElementById("vcViewerTabs")) {
      global.switchVCViewerTab(state.tab, document.querySelector('#vcViewerTabs .tab-btn[data-tab="' + state.tab + '"]'));
      return;
    }
    var pane = state.pane && document.getElementById(state.pane);
    if (!pane && state.pane) pane = document.getElementById("tab-" + tabIdFromPane({ id: state.pane }));
    if (!pane) return;
    var id = tabIdFromPane(pane);
    var button = buttonForTab(id);
    if (typeof global.switchTab === "function" && button) {
      global.switchTab(id, button);
      return;
    }
    if (pane.classList.contains("dtab-pane") && typeof global.switchDetailTab === "function" && button) {
      global.switchDetailTab(id, button);
      return;
    }
    pane.classList.add("active");
    if (button) button.classList.add("active");
  }

  function findById(items, id) {
    return (items || []).find(function (item) { return item && String(item.id) === String(id); });
  }

  function restoreSpecific(state) {
    if (state.view === "classroom" && state.classroomId && typeof global.openClassroom === "function") {
      var classroom = findById(global.__TASS_CLASSES__, state.classroomId);
      if (classroom) {
        global.openClassroom(classroom.title || classroom.subject || "Classroom", classroom.section || "", classroom.color || "", classroom.id);
        restorePane(state);
        return true;
      }
      return false;
    }
    if (state.view === "subject" && state.subjectId && typeof global.openSubject === "function") {
      var subject = findById(global.__TASS_SUBJECTS__, state.subjectId);
      if (subject) {
        global.openSubject(subject.title, subject.teacher, subject.sub, subject.color, subject.grade, subject.id, subject.teacher_id);
        restorePane(state);
        return true;
      }
      return false;
    }
    if (state.view === "classroom" && state.classroomId && typeof global.openClassroomViewer === "function") {
      global.openClassroomViewer(state.classroomId, state.tab || "students");
      return true;
    }
    if (state.view === "teacher-detail" && state.teacherDetailId && typeof global.openTeacherDetail === "function") {
      var detailId = /^\d+$/.test(state.teacherDetailId) ? Number(state.teacherDetailId) : state.teacherDetailId;
      global.openTeacherDetail(detailId);
      restorePane(state);
      return true;
    }
    return false;
  }

  function restore() {
    if (global.location.search && /notification_(tab|record|classroom)=/.test(global.location.search)) return;
    var state = read();
    if (!state) return;
    restoring = true;
    var restored = restoreSpecific(state);
    if (!restored) {
      if (document.getElementById("view-" + state.view) && typeof global.showView === "function") global.showView(state.view);
      restorePane(state);
    }
    restoring = false;
    save();
  }

  document.addEventListener("click", function () { global.setTimeout(save, 0); }, true);
  global.addEventListener("pagehide", save);
  global.addEventListener("popstate", function () { global.setTimeout(save, 0); });
  global.setInterval(save, 900);

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", restore);
  else global.setTimeout(restore, 0);
})(window, document);
