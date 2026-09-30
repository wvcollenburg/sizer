#!/usr/bin/env python3
"""Capture the screenshots of the online manual (app/templates/manual/).

    PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \\
        ~/.local/share/playwright-venv/bin/python tools/manual_shots.py \\
        --base http://127.0.0.1:5101 --demo DEMO.xlsx [--only NAME ...]

Runs against a FRESH, seeded local sizer that has these four accounts, all
with password $MANUAL_PW (default Manual-Pass1!):

    user@partnerco.example     user           (partner edition)
    admin@partnerco.example    tenant_admin   (partner edition)
    sa@scalecomputing.com      user, scale    (staff edition)
    super@scalecomputing.com   super_admin    (staff edition)

--demo is an anonymised Live Optics export, made by tools/manual_demo_data.py.
NEVER point this at a real customer file: every partner reads the manual.

Pictures land in app/manual/img/user/ (partner edition, readable by every
signed-in user) and app/manual/img/scale/ (staff edition, gated). A picture
showing a staff-only control must go to scale/. The names are the ones the
chapter templates pass to shot().

The script creates the demo projects it photographs, so run it once per fresh
database; --only re-shoots individual pictures on a database that already has
them.
"""
import argparse
import json
import os


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "app", "manual", "img")
PW = os.environ.get("MANUAL_PW", "Manual-Pass1!")

USER = "user@partnerco.example"
TADMIN = "admin@partnerco.example"
STAFF = "sa@scalecomputing.com"
SUPER = "super@scalecomputing.com"

W, H = 1440, 900
DEMO_PROJECT = "Demo Company — HQ refresh"

# While one element is captured: the sticky header and the fixed disclaimer
# must not float over it.
UNSTICK = """
header, .sizer-project-bar, .wizard-rail, .wiz-rail { position: static !important; }
.dev-disclaimer { display: none !important; }
"""


class Shooter:
    def __init__(self, pw, base, demo, only=None):
        self.base = base.rstrip("/")
        self.demo = demo
        self.only = set(only or [])
        self.browser = pw.chromium.launch()
        self.ctx = None
        self.page = None
        self.state = {}
        self.taken = []
        self._tall = False

    # ── sessions ────────────────────────────────────────────────────────────
    def login(self, email, prefs=None):
        if self.ctx:
            self.ctx.close()
        self.ctx = self.browser.new_context(viewport={"width": W, "height": H},
                                            device_scale_factor=1)
        prefs = dict({"theme": "light"}, **(prefs or {}))
        self.ctx.add_init_script(
            "try{%s}catch(e){}" % "".join(
                "localStorage.setItem(%s,%s);" % (json.dumps(k), json.dumps(v))
                for k, v in prefs.items()))
        self.page = self.ctx.new_page()
        # browser prompt()/confirm(): answer with self.answer, else accept
        self.answer = None
        self.page.on("dialog", lambda d: d.accept(self.answer) if self.answer is not None
                     else d.accept())
        if email:
            r = self.page.request.post(self.base + "/api/auth/login",
                                       data={"email": email, "password": PW})
            assert r.ok, (email, r.status, r.text())
        return self.page

    def api(self, method, path, body=None):
        r = self.page.request.fetch(self.base + path, method=method,
                                    data=json.dumps(body) if body is not None else None,
                                    headers={"Content-Type": "application/json"})
        assert r.ok, (method, path, r.status, r.text()[:300])
        return r.json() if r.text() else None

    def goto(self, path, settle=1200):
        self.page.goto(self.base + path)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(settle)

    def click(self, handler, *args, nth=0, within=None, wait=500):
        """Click the nth VISIBLE control whose data-click spec is exactly
        [handler, *args] (the delegate.js wiring every control uses)."""
        spec = [handler] + list(args)
        handle = self.page.evaluate_handle("""([spec, nth, within]) => {
            const root = within ? document.querySelector(within) : document;
            const want = JSON.stringify(spec);
            const hits = [...root.querySelectorAll('[data-click]')].filter(el => {
                try { return JSON.stringify(JSON.parse(el.dataset.click)) === want; }
                catch (e) { return false; }
            }).filter(el => el.offsetParent !== null || el.getClientRects().length);
            return hits[nth] || null;
        }""", [spec, nth, within])
        el = handle.as_element()
        assert el, "no visible control for %s" % (spec,)
        el.scroll_into_view_if_needed()
        el.click()
        self.page.wait_for_timeout(wait)

    @property
    def project(self):
        """The demo project's id: remembered, or found by name when a stage
        runs on its own against a database an earlier run populated."""
        if "project" not in self.state:
            mine = self.api("GET", "/api/projects/?scope=tenant")
            self.state["project"] = next(p["id"] for p in mine if p["name"] == DEMO_PROJECT)
        return self.state["project"]

    # ── capture ─────────────────────────────────────────────────────────────
    def wanted(self, name):
        return not self.only or name in self.only

    def shot(self, name, target=None, edition="user", full=False, pad=0, height=None):
        """Viewport (default), full page, or one element (a selector).
        `height` grows the window first, for dialogs that would scroll."""
        if not self.wanted(name):
            return
        if target and "modal" in target and not height and not self._tall:
            height = 1600       # a dialog's max-height follows the window
        if height:
            self.page.set_viewport_size({"width": W, "height": height})
            self.page.wait_for_timeout(400)
            self._tall = True
            try:
                return self.shot(name, target, edition, full, pad)
            finally:
                self._tall = False
                self.page.set_viewport_size({"width": W, "height": H})
        folder = os.path.join(OUT, edition)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name + ".png")
        # dialogs slide in; let the animation finish
        self.page.wait_for_timeout(900 if target and "modal" in target else 300)
        if target:
            loc = self.page.locator(target).first
            # A capture taller than the window gets the sticky header and
            # the fixed disclaimer stitched into it, so grow the window to
            # fit the element instead (once; the recursion restores it).
            box = loc.bounding_box()
            if box and not self._tall and box["height"] + 2 * pad > H - 120:
                return self.shot(name, target, edition, full, pad,
                                 height=int(box["height"] + 2 * pad + 240))
            # and pin the page chrome in place while the element is taken
            unstick = self.page.add_style_tag(content=UNSTICK)
            try:
                loc.scroll_into_view_if_needed()
                self.page.wait_for_timeout(150)
                box = self.page.evaluate("""(el) => {
                    const r = el.getBoundingClientRect();
                    return {x: r.left + scrollX, y: r.top + scrollY,
                            width: r.width, height: r.height};
                }""", loc.element_handle())
                self.page.screenshot(path=path, full_page=True, clip={
                    "x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
                    "width": box["width"] + 2 * pad,
                    "height": box["height"] + 2 * pad})
            finally:
                unstick.evaluate("el => el.remove()")
        else:
            self.page.evaluate("window.scrollTo(0, 0)")
            self.page.wait_for_timeout(200)
            unstick = self.page.add_style_tag(content=UNSTICK) if full else None
            try:
                self.page.screenshot(path=path, full_page=full)
            finally:
                if unstick:
                    unstick.evaluate("el => el.remove()")
        self.taken.append("%s/%s" % (edition, name))
        print("  %s/%s.png" % (edition, name))

    def modal(self, modal_id):
        return "#%s .modal-content" % modal_id

    # ── stages ──────────────────────────────────────────────────────────────
    def stage_signin(self):
        p = self.login(None)
        self.goto("/")
        self.shot("signin", self.modal("auth-modal"))
        self.click("setAuthTab", "signup")
        p.fill("#auth-full-name", "Pat Partner")
        p.fill("#auth-email", "pat@partnerco.example")
        p.fill("#auth-password", "Correct-Horse1")
        p.fill("#auth-confirm", "Correct-Horse")
        p.locator("#auth-password").dispatch_event("input")
        p.locator("#auth-confirm").dispatch_event("input")
        self.shot("signup", self.modal("auth-modal"), height=1500)

    def stage_projects(self):
        p = self.login(USER)
        self.goto("/")
        self.shot("projects-empty")
        self.click("openNewProject")
        p.fill("#new-project-name", "Demo Company — HQ refresh")
        self.shot("new-project", self.modal("new-project-modal"))
        p.keyboard.press("Escape")
        # the demo projects everything else is photographed in
        main = self.api("POST", "/api/projects/", {
            "name": DEMO_PROJECT, "customer_name": "Demo Company Ltd",
            "opportunity_ref": "OPP-2026-0142"})
        self.api("POST", "/api/projects/", {"name": "Northwind branch offices",
                                             "customer_name": "Northwind Traders"})
        self.state["project"] = main["id"]
        self.state["project_code"] = main.get("code")

    def open_new_sizing(self, mode=None):
        self.goto("/?project=%s&new=1" % self.project)
        if mode:
            self.click("requestSwitchMode", mode, wait=800)

    def upload_demo(self):
        self.page.set_input_files("#file-input", self.demo)
        self.page.wait_for_timeout(2500)

    def stage_import_fanout(self):
        p = self.login(USER)
        self.open_new_sizing()
        self.shot("sizer-appliance-empty")
        self.click("requestSwitchMode", "import", wait=800)
        self.shot("wizard-upload")
        self.upload_demo()
        self.shot("fanout", self.modal("cluster-fanout-modal"))
        self.click("createPerClusterSizings", wait=3000)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(1500)
        self.shot("project-to-be-sized")

    def sizing_id(self, name):
        proj = self.api("GET", "/api/projects/%s" % self.project)
        for sz in proj.get("sizings", []):
            if sz["name"] == name:
                return sz["id"]
        raise KeyError(name)

    def open_sizing(self, name, settle=3000):
        self.goto("/?project=%s&sizing=%s" % (self.project, self.sizing_id(name)),
                  settle=settle)

    def stage_wizard(self):
        p = self.login(USER)
        self.open_sizing("Remote-Office")
        self.shot("wizard-environment", "#import-wizard", pad=12)
        self.click("openSourceCpuModal")
        self.shot("source-cpu", self.modal("source-cpu-modal"))
        p.keyboard.press("Escape")
        self.page.evaluate("document.getElementById('source-cpu-modal').style.display='none'")
        self.click("wizardNext", wait=1200)
        self.shot("wizard-vms", "#import-wizard", pad=12)
        self.click("removeVmwareArtifacts", wait=800)
        p.fill("#vm-search", "vc")
        p.locator("#vm-search").dispatch_event("input")
        self.shot("wizard-vms-artifacts", "#vm-triage-panel", pad=8)
        p.fill("#vm-search", "")
        p.locator("#vm-search").dispatch_event("input")
        self.click("wizardNext", wait=2500)
        self.shot("wizard-sizing", "#import-wizard", pad=12)
        self.click("wizardToggleAdvanced", wait=800)
        self.shot("wizard-sizing-advanced", "#import-wizard", pad=12)
        self.click("wizardNext", wait=2500)
        self.shot("wizard-recs", "#import-wizard", pad=12)

    def stage_results(self):
        """Continues on step 5 of the wizard stage's sizing."""
        p = self.login(USER)
        self.open_sizing("Remote-Office")
        for _ in range(3):
            self.click("wizardNext", wait=1500)
        self.click("toggleRecRow", 0, wait=1200)
        self.shot("rec-card", "#primary-recommendations .rec-row.is-open", pad=10)
        self.click("openClusterDiagram", "import", 0, wait=1500)
        self.shot("network-diagram", self.modal("diagram-modal"), height=1400)
        self.page.evaluate("document.getElementById('diagram-modal').style.display='none'")
        self.click("setRecView", "split", wait=1000)
        self.shot("rec-split", "#primary-recommendations", pad=10)
        self.click("setRecView", "rows", wait=800)
        self.click("selectRec", 1, wait=1200)
        self.click("wizSaveAndClose", wait=1500)
        if p.locator("#save-choice-modal").is_visible():
            self.shot("save-choice", self.modal("save-choice-modal"))
            self.click("chooseSave", "update", wait=2500)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(1500)

    def size_imported(self, name, pick=0):
        """Open a 'To be sized' fan-out sizing, walk the wizard, save."""
        self.open_sizing(name)
        for _ in range(3):
            self.click("wizardNext", wait=1500)
        if pick:
            self.click("selectRec", pick, wait=1000)
        self.click("wizSaveAndClose", wait=1500)
        if self.page.locator("#save-choice-modal").is_visible():
            self.click("chooseSave", "update", wait=2500)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(1000)

    def stage_populate(self):
        """The rest of the demo project: sized rows, an alternative, a DR
        target and a replication link."""
        p = self.login(USER)
        self.size_imported("Production")
        self.size_imported("Test-Dev")
        pid = self.project
        # an alternative for Remote-Office: all-flash
        self.api("POST", "/api/sizings/%s/duplicate" % self.sizing_id("Remote-Office"), {})
        self.goto("/?project=%s" % pid)
        self.answer = "DR site"
        self.click("addDrTarget", wait=1500)
        self.answer = None

    def stage_alternative(self):
        """Save-as-new of Remote-Office on all-flash: the naming dialogs."""
        p = self.login(USER)
        self.open_sizing("Remote-Office")      # a saved sizing opens on step 5
        self.click("wizardGoto", 4, wait=1500)
        p.select_option("#storage-pref", "flash")
        self.page.wait_for_timeout(1500)
        self.click("wizardNext", wait=1500)
        self.click("saveCurrentSizing", wait=900)
        self.click("chooseSave", "new", wait=900)
        p.fill("#name-sizing-input", "Remote-Office all-flash")
        self.shot("name-sizing", self.modal("name-sizing-modal"))
        p.locator("#name-sizing-input").press("Enter")
        self.page.wait_for_timeout(2000)
        self.shot("saved-code", self.modal("info-modal"))
        # the plain duplicate from stage_populate is not needed any more
        try:
            self.api("DELETE", "/api/configs/%s" % self.sizing_id("Remote-Office (copy)"))
        except KeyError:
            pass

    def stage_project_view(self):
        p = self.login(USER)
        pid = self.project
        self.goto("/?project=%s" % pid, settle=2500)
        # Options panel: the alternative
        self.click("openSizingPanel", self.sizing_id("Remote-Office all-flash"), wait=900)
        p.select_option("#sizing-role", "alternative")
        p.fill("#sizing-new-tag", "Option B")
        self.click("addTagFromInput", wait=600)
        p.fill("#sizing-notes", "All-flash: lower latency for the ERP database; higher cost per TB.")
        self.shot("sizing-panel", self.modal("sizing-modal"))
        self.click("submitSizingPanel", wait=1500)
        # Option A tag + alternative role on the hybrid one
        self.click("openSizingPanel", self.sizing_id("Remote-Office"), wait=900)
        p.select_option("#sizing-role", "alternative")
        p.fill("#sizing-new-tag", "Option A")
        self.click("addTagFromInput", wait=600)
        self.click("submitSizingPanel", wait=1500)
        # Production replicates to the DR site
        self.click("openSizingPanel", self.sizing_id("Production"), wait=900)
        p.select_option("#sizing-rep-target", str(self.sizing_id("DR site")))
        self.page.wait_for_timeout(400)
        self.shot("sizing-panel-replication", self.modal("sizing-modal"))
        self.click("submitSizingPanel", wait=1500)
        self.goto("/?project=%s" % pid, settle=2500)
        self.shot("project-view")
        # batch edit
        for n in ("Production", "Test-Dev"):
            p.locator("[data-change*='\"toggleSizing\",%s,']" % self.sizing_id(n)).check()
        self.page.wait_for_timeout(500)
        self.shot("selection-bar", "#project-selection-bar", pad=8)
        self.click("openBatchEdit", wait=900)
        self.shot("batch-edit", self.modal("batch-edit-modal"))
        self.goto("/?project=%s" % pid, settle=2000)
        # details
        self.click("openProjectSettings", wait=900)
        p.fill("#ps-description", "Refresh of the three VMware clusters at HQ; DR to the co-location site.")
        self.shot("project-details", self.modal("project-settings-modal"))
        self.click("submitProjectSettings", wait=1500)
        # exports as
        self.click("openExportAs", self.sizing_id("Production"), wait=1200)
        self.shot("export-as", self.modal("export-as-modal"))

    def stage_compare(self):
        p = self.login(USER)
        pid = self.project
        self.goto("/?project=%s" % pid, settle=2500)
        self.click("openComparisons", wait=900)
        self.shot("compare-mode", self.modal("comparisons-modal"))
        self.click("cmpChooseMode", "sizings", wait=700)
        for n in ("Remote-Office", "Remote-Office all-flash"):
            p.locator("#cmp-pick-list input[data-cmp-id='%s']" % self.sizing_id(n)).check()
        self.page.wait_for_timeout(400)
        self.shot("compare-pick", self.modal("comparisons-modal"))
        p.click("#cmp-run-btn")
        self.page.wait_for_timeout(2500)
        self.shot("compare-result", self.modal("comparisons-modal"))
        p.keyboard.press("Escape")
        self.goto("/?project=%s" % pid, settle=2000)
        self.click("openComparisons", wait=900)
        self.click("cmpChooseMode", "tags", wait=700)
        for cb in p.locator("#cmp-pick-list input[data-cmp-id]").all():
            label = cb.locator("xpath=..").inner_text()
            if "Option" not in label:
                cb.uncheck()
        self.page.wait_for_timeout(300)
        p.click("#cmp-run-btn")
        self.page.wait_for_timeout(2500)
        self.shot("compare-tags", self.modal("comparisons-modal"))

    def stage_exports(self):
        p = self.login(USER)
        pid = self.project
        self.goto("/?project=%s" % pid, settle=2500)
        self.click("openExports", wait=1000)
        self.click("expChooseMode", "sizings", wait=700)
        for n in ("Production", "Remote-Office", "Test-Dev"):
            p.locator("#exp-pick-list input[data-exp-id='%s']" % self.sizing_id(n)).check()
        self.page.wait_for_timeout(300)
        self.shot("export-pick", self.modal("exports-modal"))
        p.click("#exp-run-btn")
        self.page.wait_for_timeout(2000)
        self.shot("export-downloads", self.modal("exports-modal"))

    def stage_dr(self):
        p = self.login(USER)
        self.open_sizing("DR site", settle=3500)
        self.shot("dr-target", "#dr-target-form", pad=12)

    def stage_modes(self):
        p = self.login(USER)
        # Appliance (Certified): the fixed-configuration calculator
        self.open_new_sizing()
        models = p.eval_on_selector_all("#model-select option", "os => os.map(o => o.value)")
        p.select_option("#model-select", "HC3450DF" if "HC3450DF" in models else models[1])
        self.page.wait_for_timeout(1500)
        self.shot("appliance-form", "#appliance-form", pad=12)
        self.shot("appliance-results", "#results", pad=12)
        # the unsaved-work guard
        self.click("requestSwitchMode", "validated", wait=900)
        self.shot("leave-page", self.modal("leave-page-modal"))
        self.click("chooseLeave", "cancel", wait=500)
        # Software Only (Validated)
        self.open_new_sizing("validated")
        self.shot("validated-form", "#validated-form", pad=12)
        self.click("calculateValidated", wait=2000)
        self.shot("validated-results", "#results", pad=12)
        # Manual input
        self.open_new_sizing("manual")
        for sel, val in (("#man-platform", "VMware vSphere 7"), ("#man-cluster", "Branch-01"),
                         ("#man-hosts", "2"), ("#man-cores", "32"), ("#man-threads", "64"),
                         ("#man-peak-cpu", "45"), ("#man-avg-cpu", "20"),
                         ("#man-total-vms", "24"), ("#man-active-vms", "22"),
                         ("#man-vcpus", "96"), ("#man-prov-ram", "384"), ("#man-used-ram", "300"),
                         ("#man-prov-storage", "8"), ("#man-ds-used", "6"), ("#man-ds-total", "10")):
            p.fill(sel, val)
        self.shot("manual-form", "#manual-form", pad=12)
        self.click("openManualVmModal", wait=800)
        for _ in range(3):
            self.click("addManualVm", wait=300)
        self.shot("manual-vms", self.modal("manual-vm-modal"))
        self.click("closeManualVmModal", nth=0, wait=600)
        self.click("calculateManual", wait=3000)
        self.shot("manual-results", "#sizing-results", pad=12)

    def stage_classic(self):
        self.login(USER, prefs={"sizerImportView": "classic"})
        self.open_sizing("Production", settle=3500)
        self.shot("classic-view", full=True)

    def stage_bom(self):
        p = self.login(USER)
        pid = self.project
        self.goto("/?project=%s" % pid, settle=2000)
        self.click("openBomChecker", wait=1000)
        p.select_option("#bom-sizing-select", str(self.sizing_id("Production")))
        p.fill("#bom-name", "Partner quote v1")
        for d in p.locator("#bom-modal details").all():
            d.evaluate("d => d.open = true")
        self.shot("bom-upload", self.modal("bom-modal"))
        p.set_input_files("#bom-file-input", os.path.join(
            ROOT, "tests", "fixtures", "bom", "synthetic_dell_quote.xlsx"))
        self.page.wait_for_timeout(600)
        p.click("#bom-run-btn")
        self.page.wait_for_timeout(4000)
        self.shot("bom-result", self.modal("bom-modal"))
        p.keyboard.press("Escape")
        self.goto("/?project=%s" % pid, settle=2000)
        self.shot("project-bom-list", "#project-bom-checks", pad=10)

    def stage_account(self):
        p = self.login(USER)
        self.goto("/", settle=1500)
        self.shot("header", "header", pad=0)
        self.click("openSizingsModal", wait=1200)
        self.shot("my-sizings", self.modal("sizings-modal"))
        self.page.evaluate("closeSizingsModal()")
        self.click("openPrivacyModal", wait=900)
        self.shot("privacy-data", self.modal("privacy-modal"))
        self.page.evaluate("document.getElementById('privacy-modal').style.display='none'")
        # projects home, with and without colleagues' projects
        self.shot("projects-home")

    def stage_tenant_admin(self):
        p = self.login(TADMIN)
        self.api("POST", "/api/projects/", {"name": "Contoso datacenter consolidation",
                                             "customer_name": "Contoso"})
        self.goto("/", settle=1500)
        self.shot("header-tenant-admin", "header", pad=0)
        p.check("#project-scope-all")
        self.page.wait_for_timeout(1200)
        self.shot("projects-colleagues")
        self.click("openOrgModal", wait=1200)
        self.shot("org-modal", self.modal("org-modal"))

    # ── staff edition (app/manual/img/scale/) ───────────────────────────────
    def stage_staff(self):
        p = self.login(STAFF)
        self.goto("/", settle=1500)
        self.shot("header-staff", "header", edition="scale")
        # a partner's project, opened by its share code
        self.answer = self.state.get("project_code") or self.api(
            "GET", "/api/projects/%s" % self.project)["code"]
        self.click("openProjectByCodePrompt", wait=2500)
        self.answer = None
        self.shot("staff-shared-project", edition="scale")
        # My Sizings with the retrieve-by-code box
        self.goto("/", settle=1500)
        self.click("openSizingsModal", wait=1200)
        p.fill("#sizings-code-input", "781845344368")
        self.shot("staff-my-sizings", self.modal("sizings-modal"), edition="scale")
        self.page.evaluate("closeSizingsModal()")
        # own project: the Salesforce link in Details
        own = self.api("POST", "/api/projects/", {"name": "Fabrikam — edge rollout",
                                                   "customer_name": "Fabrikam"})
        self.goto("/?project=%s" % own["id"], settle=2000)
        self.click("openProjectSettings", wait=900)
        p.fill("#ps-salesforce", "https://example.my.salesforce.com/lightning/r/Opportunity/0060000000Demo/view")
        self.shot("staff-project-details", self.modal("project-settings-modal"), edition="scale")

    def stage_admin(self):
        p = self.login(SUPER)
        self.goto("/", settle=1500)
        self.shot("header-super", "header", edition="scale")
        self.goto("/admin/", settle=2000)
        for tab in ("models", "cpus", "nics", "drives", "tuning", "pricebook", "hcl",
                    "bomreviews", "users", "stale", "tenants", "sizings", "email", "audit"):
            self.click("switchTab", tab, wait=1500)
            self.shot("admin-" + tab, edition="scale")
        self.click("switchTab", "models", wait=1200)
        p.locator("[data-click^='[\"openEditModel\"']").first.click()
        self.page.wait_for_timeout(1200)
        self.shot("admin-model-edit", "#edit-modal > div", edition="scale", height=1600)


# In this order: each stage builds on what the previous ones created.
STAGES = ("signin", "projects", "import_fanout", "wizard", "results", "populate",
          "alternative", "project_view", "compare", "exports", "dr", "modes",
          "classic", "bom", "account", "tenant_admin", "staff", "admin")


def main():
    from playwright.sync_api import sync_playwright   # the Playwright venv only

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:5101")
    ap.add_argument("--demo", required=True,
                    help="anonymised Live Optics export (tools/manual_demo_data.py)")
    ap.add_argument("--stages", nargs="*", default=list(STAGES), choices=STAGES)
    ap.add_argument("--only", nargs="*", help="re-shoot just these picture names")
    args = ap.parse_args()

    # The file name shows on screen (the Source column), so give it a neutral one.
    demo = args.demo
    if os.path.basename(demo) != "Demo_Company_LiveOptics.xlsx":
        import shutil
        import tempfile
        demo = os.path.join(tempfile.mkdtemp(), "Demo_Company_LiveOptics.xlsx")
        shutil.copy(args.demo, demo)

    with sync_playwright() as pw:
        s = Shooter(pw, args.base, demo, args.only)
        for stage in args.stages:
            print("[%s]" % stage)
            getattr(s, "stage_" + stage)()
        print("%d pictures" % len(s.taken))


if __name__ == "__main__":
    main()
