"use client";
interface Props { size?: number; showTagline?: boolean; }

export function CipherLogo({ size = 64, showTagline = true }: Props) {
  const cx = size / 2, cy = size / 2, r = size * 0.42;
  const sides = 6;
  const hexPoints = (radius: number, offset = 0) =>
    Array.from({ length: sides }, (_, i) => {
      const a = (Math.PI / 3) * i + offset;
      return `${cx + radius * Math.cos(a)},${cy + radius * Math.sin(a)}`;
    }).join(" ");

  // Binary ring characters placed around outer hex
  const binaryChars = "10110100101101001011";
  const binaryRadius = r * 1.18;

  return (
    <div style={{ display:"flex", flexDirection:"column", alignItems:"center", gap: size * 0.18 }}>
      <svg
        width={size} height={size}
        viewBox={`0 0 ${size} ${size}`}
        fill="none"
        aria-label="Cipher logo"
        style={{ display:"block" }}
      >
        {/* Outer hex — dashed gold */}
        <polygon
          points={hexPoints(r * 1.08)}
          stroke="#e8b84b"
          strokeWidth={size * 0.022}
          strokeDasharray={`${size * 0.055} ${size * 0.038}`}
          fill="none"
          opacity={0.55}
        />
        {/* Inner hex — solid cyan */}
        <polygon
          points={hexPoints(r * 0.88)}
          stroke="#00d4ff"
          strokeWidth={size * 0.025}
          fill="rgba(0,212,255,0.04)"
          opacity={0.8}
        />

        {/* Binary characters around inner hex */}
        {binaryChars.split("").map((ch, i) => {
          const angle = ((Math.PI * 2) / binaryChars.length) * i - Math.PI / 2;
          const bx = cx + binaryRadius * Math.cos(angle);
          const by = cy + binaryRadius * Math.sin(angle);
          return (
            <text key={i} x={bx} y={by}
              textAnchor="middle" dominantBaseline="middle"
              fontSize={size * 0.072} fontFamily="'JetBrains Mono',monospace"
              fill="#00d4ff" opacity={0.25} fontWeight="500"
            >{ch}</text>
          );
        })}

        {/* C letterform — bold serif */}
        <text
          x={cx - size * 0.035} y={cy + size * 0.13}
          textAnchor="middle" dominantBaseline="middle"
          fontSize={size * 0.48}
          fontFamily="Georgia, 'Times New Roman', serif"
          fill="#e8edf5"
          fontWeight="700"
          letterSpacing="-0.02em"
          style={{ userSelect:"none" }}
        >C</text>

        {/* Gold slash — ONLY through the C, not the whole hex */}
        <line
          x1={cx - size * 0.10} y1={cy + size * 0.22}
          x2={cx + size * 0.16} y2={cy - size * 0.22}
          stroke="#e8b84b"
          strokeWidth={size * 0.045}
          strokeLinecap="round"
          opacity={0.92}
        />
      </svg>

      {/* Tagline — below hex */}
      {showTagline && (
        <div style={{
          fontFamily:"'JetBrains Mono',monospace",
          fontSize: Math.max(8, size * 0.115),
          letterSpacing:"0.28em",
          color:"#546882",
          fontWeight:500,
          textTransform:"uppercase",
          userSelect:"none",
        }}>
          decode the market
        </div>
      )}
    </div>
  );
}
