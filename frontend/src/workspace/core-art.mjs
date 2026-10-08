// Original procedural filament geometry. No textures, network assets or 3D runtime.
// Continuous curves replace the coarse triangulation; depth controls light, not random flashes.
const TAU=Math.PI*2;
const STRANDS=40,STEPS=144,DEPTHS=12;
const rings=Array.from({length:STRANDS},(_,i)=>i/STRANDS*TAU);
const angles=Array.from({length:STEPS+1},(_,i)=>i/STEPS*TAU);

export function filamentPoint(u,v,time){
  const drift=time*.075;
  const ribbon=v+.34*Math.sin(2*u-drift);
  const major=.635+.028*Math.cos(3*u+drift);
  const tube=.235+.016*Math.sin(2*u-drift*.7);
  const radius=major+tube*Math.cos(ribbon);
  const x=radius*Math.cos(u),y=radius*Math.sin(u),z=tube*Math.sin(ribbon)+.055*Math.sin(3*u-drift);
  // Small continuous changes in pitch, no large rocking or abrupt random displacement.
  const pitch=.57+.035*Math.sin(time*.12),cp=Math.cos(pitch),sp=Math.sin(pitch);
  const py=y*cp-z*sp,pz=y*sp+z*cp,roll=-.24;
  const cr=Math.cos(roll),sr=Math.sin(roll),perspective=1/(1-pz*.14);
  return {x:(x*cr-py*sr)*perspective,y:(x*sr+py*cr)*perspective,z:pz};
}

export function coreGeometry(time){
  return rings.map(v=>angles.map(u=>filamentPoint(u,v,time)));
}

export function drawSilkCore(ctx,x,y,r,time){
  ctx.save();ctx.lineCap='round';ctx.lineJoin='round';
  const halo=ctx.createRadialGradient(x-r*.12,y-r*.1,0,x,y,r*1.15);
  halo.addColorStop(0,'#aac8f012');halo.addColorStop(.42,'#9ab5db0b');halo.addColorStop(1,'#9ab5db00');
  ctx.fillStyle=halo;ctx.fillRect(x-r*1.2,y-r*1.2,r*2.4,r*2.4);
  const paths=coreGeometry(time),buckets=Array.from({length:DEPTHS},()=>[]);
  paths.forEach(points=>{
    for(let i=1;i<points.length;i++){
      const a=points[i-1],b=points[i],depth=Math.max(0,Math.min(DEPTHS-1,Math.floor(((a.z+b.z)/2+.8)/1.6*DEPTHS)));
      buckets[depth].push([a,b]);
    }
  });
  // Back-to-front hairlines. Batching keeps stroke calls bounded despite finer geometry.
  buckets.forEach((segments,depth)=>{
    const light=depth/(DEPTHS-1);
    ctx.strokeStyle=`rgba(${Math.round(158+light*74)},${Math.round(177+light*62)},${Math.round(204+light*47)},${.035+light*light*.5})`;
    ctx.lineWidth=.42+light*.28;ctx.beginPath();
    for(const [a,b] of segments){ctx.moveTo(x+a.x*r,y+a.y*r);ctx.lineTo(x+b.x*r,y+b.y*r)}
    ctx.stroke();
  });
  // Only three fine traveling highlights, confined to the surface rather than scattered sparks.
  for(const strand of [3,16,29]){
    const points=paths[strand],head=(time*.012+strand*.137)%1;
    for(let j=0;j<18;j++){
      const index=Math.floor(((head+j/STEPS)%1)*STEPS),a=points[index],b=points[index+1];
      const front=Math.max(.08,Math.min(1,(a.z+.65)/1.3));
      ctx.strokeStyle=`rgba(207,226,255,${Math.sin(j/18*Math.PI)*front*.55})`;
      ctx.lineWidth=.85;ctx.beginPath();ctx.moveTo(x+a.x*r,y+a.y*r);ctx.lineTo(x+b.x*r,y+b.y*r);ctx.stroke();
    }
  }
  // Sparse pinpoints sit on the same geometry: no frame-to-frame flicker or disconnected debris.
  ctx.fillStyle='#e3edff80';ctx.beginPath();
  for(let i=0;i<32;i++){
    const p=paths[(i*13)%STRANDS][(i*37)%STEPS];if(p.z<-.15)continue;
    const radius=.42+Math.max(0,p.z)*.25;
    ctx.moveTo(x+p.x*r+radius,y+p.y*r);ctx.arc(x+p.x*r,y+p.y*r,radius,0,TAU);
  }
  ctx.fill();
  const heart=ctx.createRadialGradient(x,y,0,x,y,r*.24);
  heart.addColorStop(0,'#b4d4ff20');heart.addColorStop(.35,'#98bff60c');heart.addColorStop(1,'#98bff600');
  ctx.fillStyle=heart;ctx.fillRect(x-r*.24,y-r*.24,r*.48,r*.48);
  ctx.restore();
}
