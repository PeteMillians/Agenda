import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WidgetFrame } from "./WidgetFrame";

afterEach(cleanup);

function Broken(): never {
  throw new Error("kaboom");
}

const noop = () => {};

describe("WidgetFrame", () => {
  it("isolates a crashing widget from its neighbours", () => {
    vi.spyOn(console, "error").mockImplementation(noop);
    render(
      <>
        <WidgetFrame title="Broken" editing={false} onMove={noop} onResize={noop} onRemove={noop}>
          <Broken />
        </WidgetFrame>
        <WidgetFrame title="Healthy" editing={false} onMove={noop} onResize={noop} onRemove={noop}>
          <p>still here</p>
        </WidgetFrame>
      </>,
    );
    expect(screen.getByRole("alert").textContent).toContain("kaboom");
    expect(screen.getByText("still here")).toBeTruthy();
  });

  it("maps arrow keys to move and Shift+arrows to resize while customizing", () => {
    const onMove = vi.fn();
    const onResize = vi.fn();
    render(
      <WidgetFrame title="Tasks" editing onMove={onMove} onResize={onResize} onRemove={noop}>
        <p>body</p>
      </WidgetFrame>,
    );
    const handle = screen.getByRole("button", { name: /Move Tasks/ });
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.keyDown(handle, { key: "ArrowDown", shiftKey: true });
    expect(onMove).toHaveBeenCalledWith("right");
    expect(onResize).toHaveBeenCalledWith("down");
  });

  it("hides the editing tools outside customize mode", () => {
    render(
      <WidgetFrame title="Tasks" editing={false} onMove={noop} onResize={noop} onRemove={noop}>
        <p>body</p>
      </WidgetFrame>,
    );
    expect(screen.queryByRole("button", { name: /Remove/ })).toBeNull();
  });
});
