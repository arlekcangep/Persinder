import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchLogs = vi.fn();

vi.mock("@/api/client", () => ({
  fetchLogs: (...args: unknown[]) => fetchLogs(...args),
}));

vi.mock("vue-router", () => ({
  useRoute: () => ({ query: {} }),
}));

import SettingsView from "@/views/SettingsView.vue";

// jsdom не реализует matchMedia — тема в компоненте спрашивает системную.
if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

const EMPTY = { events: [], last_id: 0 };

async function openLogs() {
  const wrapper = mount(SettingsView, {
    global: { stubs: { RouterLink: true } },
  });
  const tab = wrapper
    .findAll("button.tab")
    .find((button) => button.text() === "Журнал");
  expect(tab).toBeDefined();
  await tab?.trigger("click");
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  fetchLogs.mockReset();
  fetchLogs.mockResolvedValue(EMPTY);
});

describe("журнал в настройках", () => {
  it("запрашивает события при открытии вкладки", async () => {
    const wrapper = await openLogs();

    expect(fetchLogs).toHaveBeenCalledWith({
      limit: 200,
      kind: undefined,
      from: undefined,
      to: undefined,
    });
    wrapper.unmount();
  });

  it("фильтрует по типу события", async () => {
    const wrapper = await openLogs();
    await wrapper.find("select").setValue("embed");
    await flushPromises();

    expect(fetchLogs).toHaveBeenLastCalledWith(
      expect.objectContaining({ kind: "embed" }),
    );
    wrapper.unmount();
  });

  it("фильтрует по датам (локальные дни → ISO для API)", async () => {
    const wrapper = await openLogs();
    const dates = wrapper.findAll('input[type="date"]');
    expect(dates).toHaveLength(2);

    await dates[0].setValue("2026-11-10");
    await dates[1].setValue("2026-11-11");
    await flushPromises();

    expect(fetchLogs).toHaveBeenLastCalledWith({
      limit: 200,
      kind: undefined,
      from: new Date("2026-11-10T00:00:00").toISOString(),
      // верхняя граница — начало следующего дня (исключительная)
      to: new Date("2026-11-12T00:00:00").toISOString(),
    });
    wrapper.unmount();
  });

  it("показывает вид события в строке журнала", async () => {
    fetchLogs.mockResolvedValue({
      events: [
        {
          id: 1,
          ts: "2026-11-11T10:00:00+00:00",
          level: "info",
          kind: "embed",
          message: "сайдкар: заметка.md (model, чанков: 2)",
        },
      ],
      last_id: 1,
    });
    const wrapper = await openLogs();

    expect(wrapper.find(".log-kind").text()).toBe("эмбед");
    expect(wrapper.find(".log-message").text()).toContain("сайдкар");
    wrapper.unmount();
  });

  it("показывает подсказку, когда фильтр ничего не нашёл", async () => {
    const wrapper = await openLogs();
    await wrapper.find("select").setValue("system");
    await flushPromises();

    expect(wrapper.find(".placeholder").text()).toContain("По выбранному фильтру");
    wrapper.unmount();
  });
});
