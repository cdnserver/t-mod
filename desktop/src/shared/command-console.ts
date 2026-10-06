import type { ServiceId } from "./contracts";

export type ConsoleCommand =
  | { kind: "service"; id: ServiceId }
  | { kind: "workspace"; id: "senate" }
  | { kind: "settings" }
  | { kind: "lock" }
  | { kind: "notifications" }
  | { kind: "bar"; value: "horizontal" | "vertical" }
  | { kind: "zoom"; value: 0.9 | 1 | 1.1 }
  | { kind: "window"; value: "minimize" | "maximize" }
  | { kind: "help" }
  | { kind: "invalid"; message: string };

/** A deliberately closed grammar: no eval, shell access, or arbitrary URL. */
export function parseConsoleCommand(input: string): ConsoleCommand {
  const [verb = "", argument = ""] = input.trim().replace(/^\//, "").toLocaleLowerCase().split(/\s+/, 2);
  if (["home", "hub", "главная"].includes(verb)) return { kind: "service", id: "home" };
  if (["atlas", "атлас"].includes(verb)) return { kind: "service", id: "atlas" };
  if (["senate", "сенат"].includes(verb)) return { kind: "workspace", id: "senate" };
  if (verb === "reactor") return { kind: "service", id: "reactor" };
  if (verb === "settings") return { kind: "settings" };
  if (verb === "lock") return { kind: "lock" };
  if (verb === "notifications") return { kind: "notifications" };
  if (verb === "bar" && ["top", "right"].includes(argument)) return { kind: "bar", value: argument === "right" ? "vertical" : "horizontal" };
  if (verb === "zoom" && ["90", "100", "110"].includes(argument)) return { kind: "zoom", value: Number(argument) / 100 as 0.9 | 1 | 1.1 };
  if (verb === "window" && ["min", "max"].includes(argument)) return { kind: "window", value: argument === "min" ? "minimize" : "maximize" };
  if (verb === "help" || verb === "?") return { kind: "help" };
  return { kind: "invalid", message: "Команда не найдена. Введите /help." };
}
