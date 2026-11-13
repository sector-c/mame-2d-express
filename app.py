#!/usr/bin/env python3
"""MAME 2D Express: select a game, provide a ROM, verify, then decode locally."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import catalog
import graphics

SUPPORTED = {"1942":"Capcom 1942","commando":"Capcom Commando"}

def report_text(game,report):
    lines=[f"{game}: {report['status'].upper()}", report["message"]]
    for r in report["rows"]:
        if r["state"] != "match":
            lines.append(f"  {r['state']}: {r['region']}/{r['name']}")
    return "\n".join(lines)

def import_from_mame(mame_exe,db_path):
    """MAME's own local XML is the source of truth; no online lookup needed."""
    with tempfile.TemporaryDirectory() as td:
        xml = Path(td) / "catalog.xml"
        with xml.open("wb") as dest:
            subprocess.run([mame_exe,"-listxml"],stdout=dest,
                           stderr=subprocess.PIPE,check=True,timeout=900)
        with catalog.connect(db_path) as db:
            return catalog.import_xml(db,xml)

def gui():
    root = tk.Tk()
    root.title("MAME 2D Express | Choose game → ROM → Decode")
    root.geometry("880x740")
    outer=ttk.Frame(root,padding=14);outer.pack(fill="both",expand=True)
    ttk.Label(outer,text="MAME 2D Express",font=("TkDefaultFont",17,"bold")).pack(anchor="w")
    ttk.Label(outer,text="1. Choose a game   2. Select the ROM ZIP   3. Verify   4. Decode / experiment").pack(anchor="w",pady=(3,10))
    db_path = catalog.DEFAULT_DB
    with catalog.connect(db_path):
        pass
    search_var=tk.StringVar()
    selected_var=tk.StringVar(value="No game selected")
    zip_var=tk.StringVar(value="No ROM ZIP selected")
    output_var=tk.StringVar(value=str(Path.home()/"MAME2DExpress_Output"))
    state={"game":None,"rom":None,"extra":[],"audit":None,"busy":False}
    q=queue.Queue()

    top=ttk.Frame(outer);top.pack(fill="x",pady=(0,8))
    ttk.Button(top,text="Import MAME XML",command=lambda: choose_xml()).pack(side="left",padx=(0,6))
    ttk.Button(top,text="Import from installed MAME",command=lambda: choose_mame()).pack(side="left")
    ttk.Label(top,text="Import once to index the MAME catalog.",foreground="#666").pack(side="left",padx=12)

    ttk.Label(outer,text="Step 1. Search and select a game",font=("TkDefaultFont",11,"bold")).pack(anchor="w",pady=(9,4))
    find=ttk.Entry(outer,textvariable=search_var);find.pack(fill="x",pady=(0,5))
    find.insert(0,"")
    tree=ttk.Treeview(outer,columns=("shortname","title","support"),show="headings",height=9,selectmode="browse")
    for col,txt,width in (("shortname","MAME ID",130),("title","Game title",500),("support","Decode support",150)):
        tree.heading(col,text=txt);tree.column(col,width=width,stretch=col=="title")
    tree.pack(fill="x")
    ttk.Label(outer,textvariable=selected_var).pack(anchor="w",pady=(5,9))

    ttk.Label(outer,text="Step 2. Select local ROM ZIP",font=("TkDefaultFont",11,"bold")).pack(anchor="w")
    file_row=ttk.Frame(outer);file_row.pack(fill="x",pady=5)
    rom_button=ttk.Button(file_row,text="Choose ROM ZIP",state="disabled")
    rom_button.pack(side="left",padx=(0,8))
    extra_button=ttk.Button(file_row,text="Add parent / BIOS ZIPs",state="disabled")
    extra_button.pack(side="left")
    ttk.Label(outer,textvariable=zip_var,wraplength=820).pack(anchor="w",pady=(0,9))

    ttk.Label(outer,text="Step 3. Verify and export",font=("TkDefaultFont",11,"bold")).pack(anchor="w",pady=3)
    buttons=ttk.Frame(outer);buttons.pack(fill="x",pady=5)
    verify_button=ttk.Button(buttons,text="Verify hashes",state="disabled")
    known_button=ttk.Button(buttons,text="Decode known layout",state="disabled")
    exp_button=ttk.Button(buttons,text="Experimental scan (untested)",state="disabled")
    for b in (verify_button,known_button,exp_button):
        b.pack(side="left",padx=(0,9))
    outrow=ttk.Frame(outer);outrow.pack(fill="x",pady=(8,6))
    ttk.Label(outrow,text="Local output:").pack(side="left")
    ttk.Entry(outrow,textvariable=output_var).pack(side="left",fill="x",expand=True,padx=8)
    ttk.Button(outrow,text="Browse",command=lambda: choose_output()).pack(side="left")
    ttk.Label(outer,text="Activity and ROM verification",font=("TkDefaultFont",10,"bold")).pack(anchor="w",pady=(8,4))
    log=tk.Text(outer,height=12,wrap="word",state="disabled")
    log.pack(fill="both",expand=True)

    def append(s):
        log.config(state="normal");log.insert("end",str(s)+"\n");log.see("end");log.config(state="disabled")
    def refresh(*_):
        with catalog.connect(db_path) as db:
            rows=catalog.games(db,search_var.get(),250)
        tree.delete(*tree.get_children())
        for g in rows:
            mode=SUPPORTED.get(g["name"], "Experimental only")
            tree.insert("", "end", iid=g["name"],values=(g["name"],g["description"],mode))
        if not rows and not search_var.get():
            append("Catalog empty. Import MAME XML or import from your installed MAME to begin.")

    def choose_xml():
        path=filedialog.askopenfilename(title="MAME -listxml output",filetypes=[("MAME XML","*.xml"),("All files","*.*")])
        if path:
            def job():
                with catalog.connect(db_path) as db: return f"Imported {catalog.import_xml(db,path)} games."
            work(job,refresh_after=True)

    def choose_mame():
        path=filedialog.askopenfilename(title="Select local MAME executable")
        if path:
            work(lambda:f"Imported {import_from_mame(path,db_path)} games from MAME.",refresh_after=True)

    def choose_game(_=None):
        ids=tree.selection()
        if not ids:return
        game=ids[0]
        state.update(game=game,rom=None,extra=[],audit=None)
        with catalog.connect(db_path) as db:
            info=db.execute("SELECT * FROM games WHERE name=?",(game,)).fetchone()
        selected_var.set(f"Selected: {info['description']} [{game}] / {info['sourcefile']}")
        zip_var.set("No ROM ZIP selected")
        rom_button.config(state="normal");extra_button.config(state="disabled")
        for b in (verify_button,known_button,exp_button):b.config(state="disabled")
        append(f"Game selected: {game}. Choose its local ROM ZIP.")

    def choose_rom():
        path=filedialog.askopenfilename(title="Choose ROM ZIP for "+state["game"],
             filetypes=[("ROM ZIP","*.zip"),("All files","*.*")])
        if path:
            state.update(rom=path,extra=[],audit=None)
            zip_var.set(path)
            verify_button.config(state="normal")
            extra_button.config(state="normal")
            exp_button.config(state="normal")
            known_button.config(state="disabled")
            append("ROM selected. Verify its hashes before trusted-layout decoding.")

    def choose_extras():
        paths=filedialog.askopenfilenames(title="Optional parent or BIOS ZIPs",
            filetypes=[("ROM ZIP","*.zip"),("All files","*.*")])
        if paths:
            state["extra"]=list(paths);state["audit"]=None
            zip_var.set(f"{state['rom']} + {len(paths)} parent/BIOS archive(s)")
            known_button.config(state="disabled")
            append(f"Added {len(paths)} supplemental archives. Re-verify.")

    def choose_output():
        path=filedialog.askdirectory(title="Local output directory")
        if path:output_var.set(path)

    def paths():
        return [state["rom"]]+list(state["extra"])
    def work(fn,refresh_after=False,on_success=None):
        if state["busy"]:return
        state["busy"]=True
        for b in (verify_button,known_button,exp_button):b.config(state="disabled")
        def task():
            try:q.put(("ok",fn(),refresh_after,on_success))
            except Exception as exc:q.put(("error",f"{type(exc).__name__}: {exc}",False,None))
        threading.Thread(target=task,daemon=True).start()
    def verify():
        game=state["game"]
        chosen=paths()
        def job():
            with catalog.connect(db_path) as db:
                r=catalog.audit(db,game,chosen)
            return r
        def success(r):
            state["audit"]=r
            append(report_text(game,r))
            if game in SUPPORTED and r["status"] != "no_metadata":
                known_button.config(state="normal")
        work(job,on_success=success)
    def extract(mode):
        game=state["game"];chosen=paths()
        dest=Path(output_var.get())/game/mode
        if mode=="experimental" and not messagebox.askyesno(
                "Unverified extraction","The experimental decoder guesses layouts. "
                "The output is NOT authentic sprite sheets. Continue?"):
            return
        def job():
            if mode=="known":
                with catalog.connect(db_path) as db:
                    result=graphics.extract_known(db,game,chosen,dest)
            else:
                result=graphics.experimental(game,chosen,dest)
            return f"{mode}: wrote {len(result.get('assets',result.get('files',[])))} sheets to {dest}"
        work(job)
    def poll():
        try:
            while True:
                kind,data,refresh_after,callback=q.get_nowait()
                state["busy"]=False
                if refresh_after:refresh()
                if kind=="error":append("ERROR: "+data);messagebox.showerror("Operation failed",data)
                else:
                    if callback:callback(data)
                    else:append(data)
                if state["rom"]:verify_button.config(state="normal");exp_button.config(state="normal")
                if state["audit"] and state["game"] in SUPPORTED:
                    known_button.config(state="normal")
        except queue.Empty:pass
        root.after(120,poll)
    search_var.trace_add("write",refresh)
    tree.bind("<<TreeviewSelect>>",choose_game)
    rom_button.config(command=choose_rom)
    extra_button.config(command=choose_extras)
    verify_button.config(command=verify)
    known_button.config(command=lambda:extract("known"))
    exp_button.config(command=lambda:extract("experimental"))
    refresh();poll();find.focus_set()
    root.mainloop()

def main():
    p=argparse.ArgumentParser(description="Local game-first MAME ROM graphics catalog")
    sub=p.add_subparsers(dest="action")
    ix=sub.add_parser("import-xml");ix.add_argument("xml")
    im=sub.add_parser("import-mame");im.add_argument("mame_exe")
    gl=sub.add_parser("games");gl.add_argument("--search",default="")
    for action in ("verify","extract","experimental"):
        s=sub.add_parser(action)
        s.add_argument("game");s.add_argument("rom")
        s.add_argument("--extra",action="append",default=[])
        if action!="verify":s.add_argument("--output",default="MAME2DExpress_Output")
    args=p.parse_args()
    try:
        if not args.action:gui();return
        if args.action=="import-mame":
            print(f"Imported {import_from_mame(args.mame_exe,catalog.DEFAULT_DB)} games");return
        with catalog.connect() as db:
            if args.action=="import-xml":
                print(f"Imported {catalog.import_xml(db,args.xml)} games");return
            if args.action=="games":
                for row in catalog.games(db,args.search):
                    print(f"{row['name']:16} {row['description']}")
                return
            paths=[args.rom]+args.extra
            if args.action=="verify":
                print(report_text(args.game,catalog.audit(db,args.game,paths)))
            elif args.action=="extract":
                print(json.dumps(graphics.extract_known(db,args.game,paths,args.output),indent=2))
            elif args.action=="experimental":
                print(json.dumps(graphics.experimental(args.game,paths,args.output),indent=2))
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        p.exit(2,f"ERROR: {exc}\n")
if __name__=="__main__":
    main()
