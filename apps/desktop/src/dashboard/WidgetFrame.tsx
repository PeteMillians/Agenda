import { Component, type ErrorInfo, type ReactNode, useState } from "react";
import type { Direction } from "./layout";

/** One broken widget must never crash the dashboard (spec §12). */
export class WidgetErrorBoundary extends Component<{ children: ReactNode; title: string }, { error?: Error }> {
  state: { error?: Error } = {};

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`widget "${this.props.title}" crashed`, error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="widget-error" role="alert">
          <p>This widget stopped working.</p>
          <p className="muted">{this.state.error.message}</p>
          <button className="quiet" onClick={() => this.setState({ error: undefined })}>
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const ARROWS: Record<string, Direction> = {
  ArrowLeft: "left",
  ArrowRight: "right",
  ArrowUp: "up",
  ArrowDown: "down",
};

export function WidgetFrame(props: {
  title: string;
  editing: boolean;
  onMove: (direction: Direction) => void;
  onResize: (direction: Direction) => void;
  onRemove: () => void;
  settings?: ReactNode;
  children: ReactNode;
}) {
  const [showSettings, setShowSettings] = useState(false);
  return (
    <section className="widget" aria-label={props.title}>
      <header className={props.editing ? "widget-head drag-handle" : "widget-head"}>
        <h3>{props.title}</h3>
        {props.editing && (
          <div className="widget-tools">
            <button
              className="quiet keyboard-move"
              aria-label={`Move ${props.title}. Arrow keys move, Shift + arrow keys resize.`}
              title="Focus, then use arrow keys to move and Shift + arrow keys to resize"
              onKeyDown={(event) => {
                const direction = ARROWS[event.key];
                if (!direction) return;
                event.preventDefault();
                (event.shiftKey ? props.onResize : props.onMove)(direction);
              }}
            >
              ⠿
            </button>
            {props.settings && (
              <button className="quiet" aria-expanded={showSettings} onClick={() => setShowSettings((s) => !s)}>
                Settings
              </button>
            )}
            <button className="quiet" aria-label={`Remove ${props.title}`} onClick={props.onRemove}>
              ✕
            </button>
          </div>
        )}
      </header>
      <div className="widget-body">
        {props.editing && showSettings ? (
          <div className="widget-settings">{props.settings}</div>
        ) : (
          <WidgetErrorBoundary title={props.title}>{props.children}</WidgetErrorBoundary>
        )}
      </div>
    </section>
  );
}
