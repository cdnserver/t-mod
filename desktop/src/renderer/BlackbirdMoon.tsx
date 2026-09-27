import { LunarTexture } from "./LunarTexture";
/* Lightweight vector fallback while the bundled high-resolution LRO map loads. */
const unit = (n: number, salt: number) => {
  const value = Math.sin(n * 127.1 + salt * 311.7) * 43758.5453;
  return value - Math.floor(value);
};
const craters = Array.from({ length: 230 }, (_, n) => {
  const angle = unit(n, 1) * Math.PI * 2;
  const distance = Math.sqrt(unit(n, 2)) * 548;
  return {
    x: 600 + Math.cos(angle) * distance,
    y: 600 + Math.sin(angle) * distance,
    radius: 3 + Math.pow(unit(n, 3), 3) * 34,
    squash: .55 + unit(n, 4) * .4,
    angle: unit(n, 5) * 180,
  };
});

function LunarFallback() {
  return (
    <svg className="bbc-moon-art" viewBox="0 0 1200 1200" aria-hidden="true">
      <defs>
        <clipPath id="bb-moon-disc"><circle cx="600" cy="600" r="566"/></clipPath>
        <radialGradient id="bb-moon-stone" cx="83%" cy="25%" r="85%">
          <stop stopColor="#c7cbd0"/><stop offset=".35" stopColor="#7e858d"/>
          <stop offset=".7" stopColor="#30363d"/><stop offset="1" stopColor="#080b10"/>
        </radialGradient>
        <radialGradient id="bb-moon-night" cx="9%" cy="65%" r="91%">
          <stop stopColor="#010205" stopOpacity=".99"/><stop offset=".46" stopColor="#010205" stopOpacity=".93"/>
          <stop offset=".75" stopColor="#010205" stopOpacity=".3"/><stop offset="1" stopColor="#010205" stopOpacity="0"/>
        </radialGradient>
        <radialGradient id="bb-crater-floor" cx="40%" cy="64%" r="64%">
          <stop stopColor="#1c232b" stopOpacity=".55"/><stop offset=".72" stopColor="#29313a" stopOpacity=".22"/>
          <stop offset=".87" stopColor="#e3e6e8" stopOpacity=".26"/><stop offset="1" stopColor="#171d24" stopOpacity=".5"/>
        </radialGradient>
        <filter id="bb-lunar-grain" x="0" y="0" width="100%" height="100%">
          <feTurbulence type="fractalNoise" baseFrequency=".045" numOctaves="4" seed="19"/>
          <feColorMatrix type="saturate" values="0"/>
          <feComposite in2="SourceGraphic" operator="in"/>
        </filter>
        <radialGradient id="bb-moon-limb" cx="50%" cy="50%" r="50%">
          <stop offset=".93" stopColor="#020408" stopOpacity="0"/>
          <stop offset=".994" stopColor="#080d13" stopOpacity=".2"/>
          <stop offset="1" stopColor="#dce7ef" stopOpacity=".65"/>
        </radialGradient>
      </defs>
      <g clipPath="url(#bb-moon-disc)">
        <circle cx="600" cy="600" r="566" fill="url(#bb-moon-stone)"/>
        <g fill="#414a54" opacity=".28">
          <ellipse cx="805" cy="380" rx="129" ry="93" transform="rotate(-28 805 380)"/>
          <ellipse cx="903" cy="628" rx="92" ry="155" transform="rotate(18 903 628)"/>
          <ellipse cx="607" cy="290" rx="96" ry="56" transform="rotate(13 607 290)"/>
          <ellipse cx="550" cy="675" rx="124" ry="143" transform="rotate(-21 550 675)"/>
        </g>
        <circle cx="600" cy="600" r="566" filter="url(#bb-lunar-grain)" opacity=".22"/>
        <g>{craters.map((c, n) => <ellipse key={n} cx={c.x} cy={c.y} rx={c.radius} ry={c.radius * c.squash} transform={`rotate(${c.angle} ${c.x} ${c.y})`} fill="url(#bb-crater-floor)"/>)}</g>
        <circle cx="600" cy="600" r="566" fill="url(#bb-moon-night)"/>
        <circle cx="600" cy="600" r="566" fill="url(#bb-moon-limb)"/>
      </g>
    </svg>
  );
}

export function BlackbirdMoon({ onPrepared, reduced = false }: { onPrepared?: (fallback: boolean) => void; reduced?: boolean }) {
  return <><LunarFallback/><LunarTexture onPrepared={onPrepared} reduced={reduced}/></>;
}
