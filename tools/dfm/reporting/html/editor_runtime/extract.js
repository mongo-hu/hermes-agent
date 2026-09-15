() => { var DfmTextRegions = (() => {
  var __defProp = Object.defineProperty;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, { get: all[name], enumerable: true });
  };
  var __copyProps = (to, from, except, desc) => {
    if (from && typeof from === "object" || typeof from === "function") {
      for (let key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(to, key) && key !== except)
          __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
    }
    return to;
  };
  var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);
  var stdin_exports = {};
  __export(stdin_exports, {
    applyDfmTextStyle: () => applyDfmTextStyle,
    cleanDfmTextStyle: () => cleanDfmTextStyle,
    collectDfmTextRegions: () => collectDfmTextRegions,
    resolveDfmText: () => resolveDfmText
  });
  function collectDfmTextRegions(root) {
    const result = [];
    const visit = (node, path) => {
      if (node.nodeType === Node.ELEMENT_NODE && /^(SCRIPT|STYLE|SVG|CANVAS|IFRAME|OBJECT|EMBED|TEMPLATE|NOSCRIPT|TEXTAREA|SELECT|OPTION)$/i.test(node.tagName)) return;
      if (node.nodeType === Node.TEXT_NODE && node.textContent?.trim()) {
        const runtimeOwned = !!node.parentElement?.closest("#activeModeLabel,[data-target],#modelIssueCallout");
        result.push({ key: `t${result.length}`, path, text: node.textContent, node, ...runtimeOwned ? { runtimeOwned: true } : {} });
        return;
      }
      Array.from(node.childNodes).forEach((child, index) => visit(child, [...path, index]));
    };
    visit(root, []);
    return result;
  }
  function cleanDfmTextStyle(input) {
    const style = {};
    if (!input || typeof input !== "object") return style;
    const value = input;
    if (typeof value.fontSize === "number" && Number.isFinite(value.fontSize) && value.fontSize >= 6 && value.fontSize <= 200) style.fontSize = value.fontSize;
    if (typeof value.fontWeight === "number" && Number.isInteger(value.fontWeight) && value.fontWeight >= 100 && value.fontWeight <= 900) style.fontWeight = value.fontWeight;
    if (typeof value.color === "string" && /^#[\da-f]{6}$/i.test(value.color)) style.color = value.color;
    if (typeof value.fontFamily === "string" && value.fontFamily.length <= 160 && /^[\p{L}\p{N}\s,"'-]+$/u.test(value.fontFamily)) style.fontFamily = value.fontFamily;
    return style;
  }
  function applyDfmTextStyle(node, style) {
    if (style.fontSize !== void 0) node.style.fontSize = style.fontSize + "px";
    if (style.fontWeight !== void 0) node.style.fontWeight = String(style.fontWeight);
    if (style.color !== void 0) node.style.color = style.color;
    if (style.fontFamily !== void 0) node.style.fontFamily = style.fontFamily;
  }
  function resolveDfmText(root, binding) {
    let node = root;
    for (const index of binding.path) node = node?.childNodes[index];
    return node?.nodeType === Node.TEXT_NODE && node.textContent === binding.text ? node : null;
  }
  return __toCommonJS(stdin_exports);
})();

window.DfmTextRegions=DfmTextRegions; return (// Shared layout extraction for the experiment and packaged production runtime.
() => {
  const styles=[...document.querySelectorAll('style')].map(n=>n.textContent).join('\n');
  const records={};
  const pages=[...document.querySelectorAll('.slide')];
  const slides=pages.map((page,i)=>{
    const children=[...page.children].filter(n=>n.matches('.element,.webgl-wrapper'));
    const elements=children.map((node,j)=>{
      const id=`dfm-${i}-${j}`, c=getComputedStyle(node);
      const text=!!node.textContent.trim() && !node.querySelector('button,a,input,select,textarea,canvas,svg,img,iframe,script,[onclick],[data-target]') &&
        !node.matches('button,.webgl-wrapper,.issue-nav-list,.finding-toolbar,.finding-cycle,[data-target],#activeModeLabel') && (node.matches('.text-box,.section-eyebrow')||!node.children.length);
      const e={id,x:node.offsetLeft,y:node.offsetTop,w:parseFloat(c.width),h:parseFloat(c.height),rotation:0,opacity:Number(c.opacity),dfmKey:id};
      if(text) Object.assign(e,{type:'text',html:node.innerHTML,fontSize:parseFloat(c.fontSize),fontFamily:c.fontFamily,fontWeight:Number(c.fontWeight)||400,color:c.color,align:c.textAlign==='center'?'center':c.textAlign==='right'?'right':'left',valign:'top',lineHeight:parseFloat(c.lineHeight)/parseFloat(c.fontSize)||1.2,letterSpacing:parseFloat(c.letterSpacing)||0});
      else Object.assign(e,{type:'shape',shape:'rect',fill:'transparent',stroke:'transparent',strokeWidth:0,radius:0});
      const clone=node.cloneNode(true);
      clone.querySelectorAll('canvas,script,iframe').forEach(n=>n.remove());
      // Runtime posters are still previews; live 3D comes from the original HTML.
      clone.querySelectorAll('.webgl-live').forEach(n=>n.classList.remove('webgl-live'));
      for(const n of [clone,...clone.querySelectorAll('*')]) for(const a of [...n.attributes]) if(/^on/i.test(a.name)) n.removeAttribute(a.name);
      clone.style.setProperty('left','0','important');clone.style.setProperty('top','0','important');
      clone.style.setProperty('right','auto','important');clone.style.setProperty('bottom','auto','important');
      clone.style.setProperty('width','100%','important');clone.style.setProperty('height','100%','important');
      clone.style.transform='none';
      const textBindings=text?[]:window.DfmTextRegions.collectDfmTextRegions(node).map(({key,path,text,runtimeOwned})=>({key,path,text,...(runtimeOwned?{runtimeOwned:true}:{})}));
      records[id]={page:i,child:j,pageClass:page.className,html:clone.outerHTML,baseline:{...e},textBindings,label:node.matches('.webgl-wrapper')?'3D':node.textContent.trim().slice(0,40)};
      // Full-page decoration stays in the slide background, not above all click targets.
      if(!text && node.matches('.shape-rect') && e.w>=page.offsetWidth*.95 && e.h>=page.offsetHeight*.95) e.locked=true;
      return e;
    });
    return {id:`dfm-page-${i}`,dfmPage:i,background:getComputedStyle(page).background,name:i===0?'报告封面':i===1?'分析总览':i===pages.length-1?'综合评估':`问题证据 ${i-1}`,elements,transition:'none',notes:'工程数据来自原始 DFM 报告，展示编辑不代表重新分析。'};
  });
  return {styles,records,slides,size:{width:pages[0].offsetWidth,height:pages[0].offsetHeight}};
}
)(); }