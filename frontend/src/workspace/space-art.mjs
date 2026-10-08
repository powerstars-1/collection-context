// Original, deterministic sky. No external images, shaders or random-per-frame particles.
function hash(x,y){const n=Math.sin(x*127.1+y*311.7)*43758.5453;return n-Math.floor(n)}
const smooth=t=>t*t*(3-2*t);
function noise(x,y){const ix=Math.floor(x),iy=Math.floor(y),u=smooth(x-ix),v=smooth(y-iy);return (hash(ix,iy)*(1-u)+hash(ix+1,iy)*u)*(1-v)+(hash(ix,iy+1)*(1-u)+hash(ix+1,iy+1)*u)*v}
function fractal(x,y){let value=0,weight=.55;for(let i=0;i<5;i++){value+=noise(x,y)*weight;x=x*2.03+5.1;y=y*2.03+3.7;weight*=.5}return value}

export function nebulaPixels(width,height){
  const pixels=new Uint8ClampedArray(width*height*4);
  for(let y=0;y<height;y++)for(let x=0;x<width;x++){
    const u=x/width,v=y/height;
    const turbulence=fractal(u*5,v*4);
    const band=v-(.78-u*.55+.055*Math.sin(u*8))+(turbulence-.5)*.15;
    const envelope=Math.exp(-band*band/.018)*(.48+.52*Math.sin(u*Math.PI));
    const clouds=fractal(u*9+turbulence*2,v*8);
    const dust=fractal(u*21,v*15);
    const light=envelope*Math.pow(Math.max(0,clouds-.24)*1.8,2)*(1-Math.pow(dust,.7)*.72);
    const cold=.5+.5*Math.sin(u*4+v*2);
    const i=(y*width+x)*4;
    pixels[i]=3+light*(36+18*(1-cold));
    pixels[i+1]=4+light*(44+11*(1-cold));
    pixels[i+2]=7+light*(63+18*cold);
    pixels[i+3]=255;
  }
  return pixels;
}

export function skyStars(width,height){
  const count=Math.max(280,Math.min(1800,Math.floor(width*height/670)));
  return Array.from({length:count},(_,i)=>{
    const near=i%31===0,middle=!near&&i%5===0,depth=near?2:middle?1:0;
    return {x:hash(i,11)*width,y:hash(i,23)*height,depth,radius:near?.85+hash(i,47)*.7:middle?.4+hash(i,47)*.35:.22+hash(i,47)*.30,
      alpha:near?.55+hash(i,37)*.4:middle?.28+hash(i,37)*.34:.12+hash(i,37)*.24,phase:hash(i,19)*Math.PI*2};
  });
}

export function starPosition(star,time,offset){
  const drift=[.05,.14,.27][star.depth],parallax=[.9,2.6,6.5][star.depth];
  return {x:star.x+Math.sin(time*.018+star.phase)*drift*8+offset.x*parallax,
    y:star.y+Math.cos(time*.014+star.phase)*drift*6+offset.y*parallax,
    alpha:star.alpha*(.90+.10*Math.sin(time*.24+star.phase))};
}

export function drawSky(ctx,texture,stars,width,height,time,offset){
  ctx.clearRect(0,0,width,height);ctx.drawImage(texture,0,0,width,height);
  // Depth and luminance buckets keep paint calls constant as star count grows.
  for(let depth=0;depth<3;depth++)for(let bucket=0;bucket<5;bucket++){
    ctx.fillStyle=depth===2?'#ecf3ff':depth===1?'#cbdcf4':'#aebfd6';
    ctx.globalAlpha=(bucket+.5)/5;ctx.beginPath();
    for(const star of stars){if(star.depth!==depth)continue;const p=starPosition(star,time,offset);if(Math.min(4,Math.floor(p.alpha*5))!==bucket)continue;ctx.moveTo(p.x+star.radius,p.y);ctx.arc(p.x,p.y,star.radius,0,Math.PI*2)}ctx.fill();
  }
  ctx.globalAlpha=1;
  for(const star of stars){if(star.depth!==2||star.alpha<.8)continue;const p=starPosition(star,time,offset),glow=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,star.radius*7);glow.addColorStop(0,'#c9deff45');glow.addColorStop(.2,'#aacaff17');glow.addColorStop(1,'#aacaff00');ctx.fillStyle=glow;ctx.fillRect(p.x-star.radius*7,p.y-star.radius*7,star.radius*14,star.radius*14)}
  // Keep the edges dark, like looking into a large space rather than a flat wallpaper.
  const vignette=ctx.createRadialGradient(width*.48,height*.48,Math.min(width,height)*.15,width*.48,height*.48,Math.max(width,height)*.65);
  vignette.addColorStop(0,'#00000000');vignette.addColorStop(1,'#000000a0');ctx.fillStyle=vignette;ctx.fillRect(0,0,width,height);
}
