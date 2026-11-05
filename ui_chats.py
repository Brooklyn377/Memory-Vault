"""Settings page, Chats: one card for everything about the chat. The Chats card holds the memory and compression controls and limits, what
the Assistant reads for each question, and Web access (its controls and limits). It is placed right after the model cards."""

HTML = r"""
<script>
(function(){
// How much the Assistant remembers of a chat, and compressing it now.
// An on/off setting kept as a limit with the value 1 or 0: it saves by itself, so it is not an unsaved change on the page.
function toggle(key,label){
 const msg=h("div",{class:"mut"}),box=h("input",{type:"checkbox","data-instant":"1"});
 api("/api/tuning").then(function(d){const r=d.items.find(function(x){return x.key===key});if(r)box.checked=Number(r.saved==null?r.default:r.saved)===1}).catch(function(){});
 box.onchange=async function(){msg.textContent="";box.disabled=true;
  try{await api("/api/tuning/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({values:{[key]:box.checked?1:0},confirmed:true})});msg.textContent="Saved."}
  catch(e){msg.textContent=e.message;box.checked=!box.checked}box.disabled=false};
 return h("div",{style:"margin-top:10px"},h("label",{class:"mut",style:"display:flex;gap:8px;align-items:center"},box,label),msg)}
function memoryBlock(){
 const msg=h("div",{class:"mut"});
 const b=h("button",{style:"padding:4px 10px;font-size:13px"},"Compress chat memory now");
 b.onclick=async function(){
  if(!await window.warnPop("Compress the chat memory now?",["The Assistant summarizes your older chats in its idle time, the same way as when the memory passes its size. If the memory is still too big afterwards, the oldest summaries are moved to the vault.","Your chats stay in the chat history and in raw/conversations."],"Compress now","Cancel"))return;
  b.disabled=true;msg.textContent="";
  try{await api("/api/chat/compress",{method:"POST"});msg.textContent="Queued: it runs in the Assistant's idle time."}catch(e){msg.textContent=e.message}b.disabled=false};
 return h("div",{style:"margin-top:10px"},toggle("assistant.show_server_tokens","Show the server's token count above the chat"),h("div",{style:"margin-top:6px"},b),msg)}
function sec(title,nodes){const d=h("details",{class:"sec","data-saves":"1"},h("summary",{},title));nodes.forEach(function(n){d.append(n)});return d}
// The limits of one group, as the rows of a section of this card (without the extra fold buildTuning gives them).
async function limits(keys,title){const d=await window.buildTuning(keys,title);return Array.from(d.children).slice(1)}
(async function(){
 const card=h("div",{class:"card"},h("h3",{},"Chats"));
 card.append(
  sec("Memory and compression",[memoryBlock(),...await limits(["assistant.history","assistant.memory_tokens","assistant.memory_shrink","assistant.summary_words","assistant.chars_per_token"])]),
  sec("Notes and messages",[toggle("chat.scrub","Hide secrets in chat (tokens, keys, passwords): what you type, what the Assistant reads, and the saved chat"),...await limits(["chat.notes","chat.hop_notes","chat.linked","chat.note_chars","chat.linked_chars","chat.page_messages","chat.fix_tries"])]),
  sec("Web access",[window.webAccessNode||h("div",{class:"mut"},"Loading..."),...await limits(["web.search_results","web.timeout","web.max_mb","assistant.web_calls"])]));
 for(let i=0;i<200&&!$("#mm");i++)await new Promise(function(r){setTimeout(r,50)});   // the model cards are drawn by the script after this one
 const mm=$("#mm");
 if(mm)mm.parentNode.insertBefore(card,mm.nextSibling);else $("main").append(card)})();
})();
</script>
"""
