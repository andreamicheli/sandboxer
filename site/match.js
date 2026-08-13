const text=(node,value)=>{node.textContent=value};
const safeId=value=>/^[a-z0-9-]{1,80}$/.test(value)?value:null;

function renderModel(node,model){
  node.style.setProperty('--model-accent',model.accent);
  text(node.querySelector('.model-maker'),`${model.producer} / MODEL`);
  text(node.querySelector('h1'),model.public_name);
  const image=node.querySelector('img'); image.src=model.art; image.alt=`Artwork for ${model.public_name}`;
  const facts=[];
  if(model.context_length!=null) facts.push(['Context length',model.context_length]);
  if(model.weights!=null) facts.push(['Open weights',model.weights?'Yes':'No']);
  node.querySelector('.technical-facts').replaceChildren(...facts.map(([label,value])=>{
    const row=document.createElement('div'),key=document.createElement('span'),val=document.createElement('b');
    text(key,label);text(val,String(value));row.append(key,val);return row;
  }));
}

function renderComparison(match){
  const target=document.querySelector('#comparison'),[left,right]=match.models;
  const leftBench=new Map((left.benchmarks||[]).map(row=>[row.id,row]));
  const comparable=(right.benchmarks||[]).filter(row=>leftBench.has(row.id));
  if(!comparable.length){const empty=document.createElement('p');empty.className='empty-data';text(empty,'No comparable, source-verified benchmark snapshot is available for both models yet. The chart is intentionally omitted.');target.replaceChildren(empty);return}
  target.replaceChildren(...comparable.map(row=>{const other=leftBench.get(row.id),card=document.createElement('article'),title=document.createElement('div');title.className='comparison-title';text(title,row.label);card.append(title);for(const [model,value] of [[left,other.value],[right,row.value]]){const line=document.createElement('div'),name=document.createElement('span'),score=document.createElement('b');text(name,model.public_name);text(score,String(value));line.append(name,score);card.append(line)}return card}));
}

function render(match){
  text(document.querySelector('#series-label'),match.series_label);text(document.querySelector('#round-label'),match.round_label);text(document.querySelector('#match-format'),match.format);text(document.querySelector('#match-id'),match.id.toUpperCase());
  text(document.querySelector('#publication-state'),match.publication_state==='published'?'PUBLISHED':'FIXTURE / NOT A RESULT');
  renderModel(document.querySelector('#model-left'),match.models[0]);renderModel(document.querySelector('#model-right'),match.models[1]);
  text(document.querySelector('#fixture-warning'),match.publication_state==='published'?'This view is generated from the frozen public evidence bundle.':'This is a design fixture. It contains no live Match result and makes no capability claim.');
  document.querySelector('#timeline-list').replaceChildren(...match.timeline.map((item,index)=>{const row=document.createElement('article'),number=document.createElement('span'),body=document.createElement('div'),label=document.createElement('small'),title=document.createElement('div'),desc=document.createElement('p');title.className='timeline-title';text(number,String(index+1).padStart(2,'0'));text(label,item.state.replaceAll('-',' ').toUpperCase());text(title,item.phase);text(desc,item.description);body.append(label,title,desc);row.append(number,body);return row}));
  renderComparison(match);const links=document.querySelector('#evidence-links'),entries=Object.entries(match.links||{});links.replaceChildren(...entries.map(([label,url])=>{const link=document.createElement('a');link.href=url;text(link,label);return link}));if(!entries.length){const note=document.createElement('p');text(note,'Report, replay, video and public evidence links will appear after publication approval.');links.append(note)}
}

async function start(){const id=safeId(new URLSearchParams(location.search).get('id')||'fixture-deepseek-vs-mimo');try{const response=await fetch('data/matches.json',{cache:'no-store'});if(!response.ok)throw new Error();const data=await response.json(),match=data.matches.find(item=>item.id===id);if(!match)throw new Error();render(match)}catch{const root=document.querySelector('#match-root'),message=document.createElement('p');message.className='load-error';text(message,'The requested public Match could not be loaded.');root.replaceChildren(message)}}
start();
