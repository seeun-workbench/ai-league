window.LEAGUE_DAYS=["2026-11-27", "2026-11-26", "2026-11-25", "2026-11-24", "2026-11-23", "2026-11-20", "2026-11-19", "2026-11-18", "2026-11-17", "2026-11-16", "2026-11-13", "2026-11-12", "2026-11-11", "2026-11-10", "2026-11-09", "2026-11-03", "2026-10-30", "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-21", "2026-10-20", "2026-10-19", "2026-10-16"];
(function(){
var el=document.getElementById('daynav');if(!el)return;
var days=window.LEAGUE_DAYS||[],cur=el.dataset.day,base=el.dataset.base,home=el.dataset.home;
if(!days.length)return;
var latest=days[0],i=days.indexOf(cur),W='일월화수목금토';
function p(s){var a=s.split('-');return[+a[0],+a[1],+a[2]];}
function wd(s){var a=p(s);return W[new Date(a[0],a[1]-1,a[2]).getDay()];}
function sh(s){var a=p(s);return a[1]+'/'+a[2];}
function url(s){return s===latest?home:base+s+'.html';}
function step(s,label,txt){
  if(!s){var o=document.createElement('span');o.className='off';o.textContent=txt.replace('-','').trim();return o;}
  var a=document.createElement('a');a.href=url(s);a.textContent=txt.replace('-',sh(s));a.setAttribute('aria-label',label+' '+sh(s));return a;}
var older=i>=0?days[i+1]:null,newer=i>0?days[i-1]:null;
var sel=document.createElement('select');sel.setAttribute('aria-label','날짜 선택');
var g=null,gm='';
days.forEach(function(s){
  var a=p(s),m=a[0]+'년 '+a[1]+'월';
  if(m!==gm){g=document.createElement('optgroup');g.label=m;sel.appendChild(g);gm=m;}
  var o=document.createElement('option');o.value=s;o.textContent=a[1]+'월 '+a[2]+'일 ('+wd(s)+')'+(s===latest?' 최신':'');
  if(s===cur)o.selected=true;g.appendChild(o);});
sel.onchange=function(){location.href=url(sel.value);};
el.textContent='';
el.appendChild(step(older,'이전 기록','‹ -'));el.appendChild(sel);el.appendChild(step(newer,'다음 기록','- ›'));
if(cur!==latest){var n=document.createElement('p');n.className='daynote';
  n.innerHTML='<a href="'+home+'">최신 기록 보기</a>';el.after(n);}
})();
