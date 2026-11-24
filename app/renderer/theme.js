(function(){
"use strict";
const KEY="kivi.theme.v2";
const PRESETS={
 dark:{bg:"#171b22",surface:"#20252e",surface2:"#1b2029",border:"#2c333d",text:"#e8ecf1",dim:"#9aa4b2",accent:"#f0703c",bubbleUser:null,bubbleKivi:null,alpha:1,radius:16,font:"system",sync:true},
 light:{bg:"#f2f4f8",surface:"#ffffff",surface2:"#e9edf3",border:"#d4dae3",text:"#1b2029",dim:"#5b6472",accent:"#e05f2a",bubbleUser:null,bubbleKivi:"#e9edf3",alpha:1,radius:16,font:"system",sync:true},
 green:{bg:"#101a14",surface:"#18241c",surface2:"#131d17",border:"#24382c",text:"#e6f2e9",dim:"#93a89b",accent:"#22c55e",bubbleUser:null,bubbleKivi:null,alpha:1,radius:16,font:"system",sync:true},
 blue:{bg:"#0f1620",surface:"#161f2b",surface2:"#121924",border:"#233242",text:"#e6eef6",dim:"#92a4b8",accent:"#38bdf8",bubbleUser:null,bubbleKivi:null,alpha:1,radius:16,font:"system",sync:true},
 red:{bg:"#1a1114",surface:"#24181c",surface2:"#1e1417",border:"#3a2429",text:"#f6e9ec",dim:"#b09a9f",accent:"#ef4444",bubbleUser:null,bubbleKivi:null,alpha:1,radius:16,font:"system",sync:true}
};
const FONTS={system:'system-ui,"Segoe UI",Tahoma,sans-serif',tahoma:"Tahoma,sans-serif",verdana:"Verdana,sans-serif",georgia:"Georgia,serif"};
function hexRgb(h){h=h.replace("#","");if(h.length===3)h=h.split("").map(c=>c+c).join("");const n=parseInt(h,16);return[(n>>16)&255,(n>>8)&255,n&255];}
function rgbStr(h){return hexRgb(h).join(",");}
function lum(h){const[r,g,b]=hexRgb(h).map(v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);});return 0.2126*r+0.7152*g+0.0722*b;}
function onColor(h){return lum(h)>0.45?"#101418":"#ffffff";}
function hslHex(h,s,l){s/=100;l/=100;const k=n=>(n+h/30)%12,a=s*Math.min(l,1-l),f=n=>l-a*Math.max(-1,Math.min(k(n)-3,Math.min(9-k(n),1)));const to=x=>Math.round(255*x).toString(16).padStart(2,"0");return"#"+to(f(0))+to(f(8))+to(f(4));}
function load(){try{const s=JSON.parse(localStorage.getItem(KEY));if(s&&PRESETS[s.preset])return Object.assign({},PRESETS[s.preset],s);}catch(e){}return Object.assign({preset:"dark"},PRESETS.dark);}
let state=load();
function resolved(){
 const accent=state.accent||PRESETS.dark.accent;
 const bu=state.sync?accent:(state.bubbleUser||accent);
 const bk=state.bubbleKivi||state.surface||PRESETS.dark.surface;
 return{accent,bu,bk};
}
function apply(){
 const r=document.documentElement.style,{accent,bu,bk}=resolved();
 const alpha=Math.min(1,Math.max(0.3,+state.alpha||1));
 r.setProperty("--bg-rgb",rgbStr(state.bg));r.setProperty("--surface-rgb",rgbStr(state.surface));
 r.setProperty("--surface2-rgb",rgbStr(state.surface2||state.surface));
 r.setProperty("--border",state.border);r.setProperty("--text",state.text);r.setProperty("--text-dim",state.dim);
 r.setProperty("--accent",accent);r.setProperty("--accent-rgb",rgbStr(accent));r.setProperty("--accent-contrast",onColor(accent));
 r.setProperty("--bubble-user",bu);r.setProperty("--bubble-user-text",onColor(bu));
 r.setProperty("--bubble-kivi",bk);r.setProperty("--bubble-kivi-text",onColor(bk));
 r.setProperty("--bg-alpha",alpha);r.setProperty("--surface-alpha",Math.min(1,alpha+0.12));
 r.setProperty("--radius",( +state.radius||16)+"px");r.setProperty("--font-ui",FONTS[state.font]||FONTS.system);
 r.setProperty("--bg",`rgba(${rgbStr(state.bg)},${alpha})`);
 r.setProperty("--bg-soft",`rgba(${rgbStr(state.surface2||state.surface)},${Math.min(1,alpha+0.12)})`);
 r.setProperty("--card",`rgba(${rgbStr(state.surface)},${Math.min(1,alpha+0.12)})`);
 r.setProperty("--card-2",`rgba(${rgbStr(state.surface2||state.surface)},${Math.min(1,alpha+0.12)})`);
 r.setProperty("--muted",state.dim);r.setProperty("--accent-soft",`rgba(${rgbStr(accent)},.16)`);
 r.setProperty("--glow",`rgba(${rgbStr(accent)},.18)`);
 localStorage.setItem(KEY,JSON.stringify(state));
 try{if(window.kivi&&window.kivi.send)window.kivi.send("theme-alpha",{alpha});}catch(e){}
 syncInputs();
}
function syncInputs(){
 document.querySelectorAll("[data-theme-key]").forEach(el=>{const k=el.dataset.themeKey;if(el.type==="color"){const r=resolved();el.value=state[k]||(k==="bubbleUser"?r.bu:k==="bubbleKivi"?r.bk:"#000000");}});
 const a=document.querySelector("[data-theme-alpha]");if(a)a.value=Math.round((+state.alpha||1)*100);
 const av=document.querySelector("[data-theme-alpha-val]");if(av)av.textContent=Math.round((+state.alpha||1)*100)+"%";
 const rd=document.querySelector("[data-theme-radius]");if(rd)rd.value=+state.radius||16;
 const rv=document.querySelector("[data-theme-radius-val]");if(rv)rv.textContent=(+state.radius||16)+"px";
 const f=document.querySelector("[data-theme-font]");if(f)f.value=state.font||"system";
 const s=document.querySelector("[data-theme-sync]");if(s)s.checked=!!state.sync;
 document.querySelectorAll(".theme-swatch").forEach(sw=>sw.classList.toggle("active",sw.dataset.preset===state.preset));
}
function randomize(){
 const h=Math.floor(Math.random()*360);
 state=Object.assign(state,{preset:"dark",bg:hslHex(h,25,10),surface:hslHex(h,22,14),surface2:hslHex(h,22,12),border:hslHex(h,20,24),text:hslHex(h,15,92),dim:hslHex(h,12,64),accent:hslHex(h,80,60),bubbleUser:null,bubbleKivi:null,sync:true});
 apply();
}
function exportTheme(){
 const blob=new Blob([JSON.stringify(state,null,2)],{type:"application/json"});
 const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="kivi-theme.json";a.click();URL.revokeObjectURL(a.href);
}
function importTheme(){
 const inp=document.createElement("input");inp.type="file";inp.accept=".json,application/json";
 inp.onchange=()=>{const f=inp.files[0];if(!f)return;const r=new FileReader();r.onload=()=>{try{const s=JSON.parse(r.result);if(s&&PRESETS[s.preset]){state=Object.assign({},PRESETS[s.preset],s);apply();}}catch(e){}};r.readAsText(f);};
 inp.click();
}
function bind(){
 document.querySelectorAll("[data-theme-key]").forEach(el=>el.addEventListener("input",()=>{state[el.dataset.themeKey]=el.value;if(el.dataset.themeKey==="bubbleUser"||el.dataset.themeKey==="bubbleKivi")state.sync=false;apply();}));
 const a=document.querySelector("[data-theme-alpha]");if(a)a.addEventListener("input",()=>{state.alpha=(+a.value)/100;apply();});
 const rd=document.querySelector("[data-theme-radius]");if(rd)rd.addEventListener("input",()=>{state.radius=+rd.value;apply();});
 const f=document.querySelector("[data-theme-font]");if(f)f.addEventListener("change",()=>{state.font=f.value;apply();});
 const s=document.querySelector("[data-theme-sync]");if(s)s.addEventListener("change",()=>{state.sync=s.checked;apply();});
 document.querySelectorAll(".theme-swatch").forEach(sw=>sw.addEventListener("click",()=>{state=Object.assign({preset:sw.dataset.preset},PRESETS[sw.dataset.preset]);apply();}));
 const on=(id,fn)=>{const el=document.getElementById(id);if(el)el.addEventListener("click",fn);};
 on("theme-reset",()=>{state=Object.assign({preset:state.preset||"dark"},PRESETS[state.preset||"dark"]);apply();});
 on("theme-random",randomize);
 on("theme-export",exportTheme);
 on("theme-import",importTheme);
}
window.KiviTheme={apply,randomize,exportTheme,importTheme,get state(){return state;},presets:PRESETS};
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",()=>{bind();apply();});else{bind();apply();}
})();
