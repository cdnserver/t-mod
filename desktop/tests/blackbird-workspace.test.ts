import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { canEnterWorkspace, workspaceForService } from "../src/shared/workspaces";
import { WorkspaceHome, WorkspaceIntro, WorkspaceNavigation } from "../src/renderer/BlackbirdWorkspace";
import type { DesktopNotification } from "../src/shared/contracts";

const access = new Map(["atlas","reactor","consensus","tasks","sgl","ovr","games","admin"].map(id => [id,{enabled:true,reason:null}]));
const home = {name:"Роберт",access,notifications:[] as DesktopNotification[],overlayEnabled:false,onOpen:async()=>{},onOverlay:()=>{},onCommunicate:()=>{},onNotifications:()=>{}};
const nav = {active:"home" as const,access,collapsed:false,online:true,onHome:()=>{},onOverview:()=>{},onSwitch:()=>{},onOpen:async()=>{},onOverlay:()=>{},onCommunicate:()=>{},onToggle:()=>{}};
describe("isolated Blackbird workspaces", () => {
  it("maps every remote service into one workspace, leaving the hub neutral", () => {
    expect(workspaceForService("home")).toBeNull();
    expect(workspaceForService("atlas")).toBe("atlas");
    for (const id of ["reactor","consensus","ovr","sgl","tasks","games","admin"] as const) expect(workspaceForService(id)).toBe("senate");
  });
  it("does not grant workspace access based on unrelated services", () => {
    const onlyAtlas = new Map([["atlas",{enabled:true}]]);
    expect(canEnterWorkspace("atlas",onlyAtlas)).toBe(true);
    expect(canEnterWorkspace("senate",onlyAtlas)).toBe(false);
    expect(canEnterWorkspace("atlas",new Map())).toBe(false);
  });
  it("uses Atlas navigation without Senate service menus", () => {
    const html = renderToStaticMarkup(createElement(WorkspaceNavigation,{...nav,space:"atlas"}));
    expect(html).toContain("Atlas AI"); expect(html).toContain("Игровой оверлей");
    expect(html).not.toContain("Личный реактор"); expect(html).not.toContain("Внешняя разведка");
    expect(html).toContain("Перейти в Сенат");
  });
  it("groups Senate institutions separately from intelligence", () => {
    const html = renderToStaticMarkup(createElement(WorkspaceNavigation,{...nav,space:"senate"}));
    for (const label of ["УЧАСТИЕ","ИНСТИТУТЫ","Личный реактор","Консенсус","Правовое бюро","Внешняя разведка"]) expect(html).toContain(label);
    expect(html).not.toContain("Игровой оверлей"); expect(html).toContain("Перейти в Atlas");
  });
  it("disables unavailable services and switching to an ungranted space", () => {
    const html = renderToStaticMarkup(createElement(WorkspaceNavigation,{...nav,space:"senate",access:new Map([["reactor",{enabled:true,reason:null}]])}));
    expect(html).toContain('title="Доступ не выдан"'); expect(html.match(/disabled=""/g)?.length).toBeGreaterThan(5);
  });
  it.each(["atlas","senate"] as const)("provides a distinct %s landing page and skippable intro",space => {
    const html = renderToStaticMarkup(createElement(WorkspaceHome,{...home,space}));
    expect(html).toContain(`space-${space}`); expect(html).toContain("Пока всё спокойно");
    const intro = renderToStaticMarkup(createElement(WorkspaceIntro,{space,reduced:false,onComplete:()=>{}}));
    expect(intro).toContain('aria-modal="true"'); expect(intro).toContain("Пропустить");
    expect(intro).toContain(space === "atlas" ? "bbw-celestial" : "bbw-senate-assembly");
  });
  it("filters and sorts actual events by workspace instead of inventing activity", () => {
    const event = {id:1,title:"Заседание",body:"Результаты",route:"/consensus",severity:"info",kind:"test",created_at:"2026-09-27T10:00:00Z",read_at:null} as DesktopNotification;
    const notices = [event,{...event,id:2,title:"Ответ Atlas",route:"/atlas"}];
    const html = renderToStaticMarkup(createElement(WorkspaceHome,{...home,space:"atlas",notifications:notices}));
    expect(html).toContain("Ответ Atlas"); expect(html).not.toContain("Заседание");
  });
});
