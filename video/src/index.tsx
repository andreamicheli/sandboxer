import React from 'react';
import {AbsoluteFill, Composition, Sequence, registerRoot} from 'remotion';

type Scene={type:'cold_open'|'model_cards_and_rules'|'match'|'intermission'|'factual_recap';duration_frames:number;event_ids:string[];identities?:string[]};
type Manifest={fps:number;identities:string[];layout:{split:{left:number;right:number;permanent:true}};scenes:Scene[];source_bundle_hash:string};

const Panel:React.FC<{name:string;side:'left'|'right'}>=({name,side})=><div style={{width:'50%',height:'100%',boxSizing:'border-box',padding:36,borderRight:side==='left'?'1px solid #2463ad':undefined,background:side==='left'?'linear-gradient(135deg,#050b18,#071b35)':'linear-gradient(225deg,#120b08,#291207)',color:'#f0eee7',fontFamily:'monospace'}}><header style={{color:side==='left'?'#4d9cff':'#ff8a3d'}}>{name}</header><pre aria-label={`${name} sanitized terminal`}>SANITIZED REPLAY</pre></div>;

export const SeriesVideo:React.FC<{manifest:Manifest}>=({manifest})=><AbsoluteFill style={{background:'#030711'}}>{manifest.scenes.reduce<{at:number;nodes:React.ReactNode[]}>((state,scene,index)=>{state.nodes.push(<Sequence key={`${scene.type}-${index}`} from={state.at} durationInFrames={scene.duration_frames}><AbsoluteFill style={{display:'flex'}}><Panel name={manifest.identities[0]} side="left"/><Panel name={manifest.identities[1]} side="right"/></AbsoluteFill></Sequence>);state.at+=scene.duration_frames;return state},{at:0,nodes:[]}).nodes}</AbsoluteFill>;

const defaultManifest:Manifest={fps:30,identities:['MODEL ONE','MODEL TWO'],source_bundle_hash:'runtime-required',layout:{split:{left:.5,right:.5,permanent:true}},scenes:[{type:'factual_recap',duration_frames:1,event_ids:['runtime-required']}]};
export const RemotionRoot:React.FC=()=> <Composition id="SandboxerSeries" component={SeriesVideo} width={1920} height={1080} defaultProps={{manifest:defaultManifest}} calculateMetadata={({props})=>({fps:props.manifest.fps,durationInFrames:props.manifest.scenes.reduce((sum,scene)=>sum+scene.duration_frames,0)})}/>;
registerRoot(RemotionRoot);
