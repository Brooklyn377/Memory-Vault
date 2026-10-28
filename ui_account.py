"""Settings page, account part: change username and password."""
import asyncio
import re

from fastapi import APIRouter, HTTPException, Request

import accounts

USER = re.compile(r"[A-Za-z0-9._-]{3,32}")
MIN_PW = 10


def register(app, authed):
    router = APIRouter()

    @router.post("/api/account/change")
    async def change(request: Request):
        if not authed(request):
            raise HTTPException(status_code=401, detail="login required")
        import web
        d = await request.json()
        cur, new_pw, again = str(d.get("current", "")), str(d.get("password", "")), str(d.get("confirm", ""))
        old_user = accounts.current_user(web.UI_USER)
        _, ok = accounts.verify(old_user, cur, web.UI_USER, web.UI_PASSWORD)
        if not ok:
            await asyncio.sleep(1)
            web.log("failed account change (wrong current password)")
            raise HTTPException(status_code=403, detail="current password is wrong")
        user = str(d.get("username", "")).strip() or old_user
        if not USER.fullmatch(user):
            raise HTTPException(status_code=400, detail="username: 3 to 32 letters, digits, . _ -")
        if new_pw:
            if len(new_pw) < MIN_PW:
                raise HTTPException(status_code=400, detail=f"password must be at least {MIN_PW} characters")
            if new_pw != again:
                raise HTTPException(status_code=400, detail="the two new passwords do not match")
        elif user == old_user:
            raise HTTPException(status_code=400, detail="nothing to change")
        accounts.save(user, new_pw or cur)
        keep = request.cookies.get("session")
        for t in list(web.SESSIONS):
            if t != keep:
                del web.SESSIONS[t]
        web.log("account changed; other sessions signed out")
        return {"ok": True}

    app.include_router(router)


HTML = r"""
<div id="ac"></div>
<script>
(function(){
const f=(t,ph)=>h("input",{type:t,placeholder:ph,autocomplete:"off"});
const cur=f("password","Current password"),usr=f("text","New username (leave blank to keep)"),
 pw=f("password","New password (leave blank to keep)"),pw2=f("password","Repeat new password"),msg=h("div",{class:"mut"});
const go=async()=>{msg.textContent="";
 const r=await fetch("/api/account/change",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({current:cur.value,username:usr.value,password:pw.value,confirm:pw2.value})});
 const j=await r.json().catch(()=>({}));
 if(r.ok){msg.textContent="Saved. Other devices were signed out.";[cur,usr,pw,pw2].forEach(e=>e.value="")}
 else msg.textContent=j.detail||"Could not save"};
$("#ac").replaceChildren(h("div",{class:"card"},h("h3",{},"Account"),
 h("div",{class:"mut"},"Passwords: at least 10 characters. You stay signed in here; every other device is signed out."),
 cur,usr,pw,pw2,h("button",{class:"pri",onclick:go},"Save"),msg));
})();
</script>
"""
