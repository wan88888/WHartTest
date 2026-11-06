"""Browser regression for project switching. Run with Python Playwright and BASE_URL pointing at the frontend."""
import os
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def check_project_switch(browser):
    projects = [{"id": 101, "name": "Project A"}, {"id": 102, "name": "Project B"}]
    user = {"id": 11, "username": "tester", "is_superuser": True}
    pending = []
    hold_a = False

    def reply(route, data):
        route.fulfill(json={"status": "success", "code": 200, "message": "ok", "data": data})

    def handle(route):
        url = urlsplit(route.request.url)
        if not url.path.startswith("/api/"):
            return route.fallback()
        if url.path == "/api/projects/":
            return reply(route, projects)
        if url.path == "/api/ui-automation/pages/":
            project = int(parse_qs(url.query)["project"][0])
            row = {"id": project, "project": project, "module": 1, "name": f"Page {project}"}
            result = {"count": 1, "results": [row]}
            if hold_a and project == 101:
                pending.append((route, result))
                return
            return reply(route, result)
        return reply(route, [])

    context = browser.new_context(locale="zh-CN")
    try:
        import json
        session = {"auth-user": json.dumps(user), "auth-accessToken": "fixture", "auth-isAuthenticated": "true",
                   "selected_project_id": "102"}
        context.add_init_script(f"Object.entries({json.dumps(session)}).forEach(([k,v]) => localStorage.setItem(k,v))")
        context.route("**/api/**", handle)
        page = context.new_page()
        page.goto(os.getenv("BASE_URL", "http://localhost:8913").rstrip("/") + "/ui-automation")
        panel = page.locator(".page-management:visible")
        expect(panel.get_by_text("Page 102", exact=True)).to_be_visible()

        def select(label):
            page.locator(".project-selector .arco-select-view").first.click()
            page.locator(".arco-select-option:visible").get_by_text(label, exact=True).click()

        hold_a = True
        with page.expect_request(lambda r: "/pages/?" in r.url and "project=101" in r.url):
            select("Project A")
        with page.expect_response(lambda r: "/pages/?" in r.url and "project=102" in r.url):
            select("Project B")
        expect(panel.get_by_text("Page 102", exact=True)).to_be_visible()
        assert pending
        with page.expect_event("requestfinished", predicate=lambda r: "/pages/?" in r.url and "project=101" in r.url):
            for route, result in pending:
                reply(route, result)
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(panel.get_by_text("Page 101", exact=True)).to_have_count(0)
        expect(panel.get_by_role("row").filter(has_text="Page 102").get_by_role("button", name="删除")).to_be_visible()
    finally:
        context.close()


if __name__ == "__main__":
    with sync_playwright() as playwright:
        with playwright.chromium.launch() as browser:
            check_project_switch(browser)
