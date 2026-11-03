type BrandMarkProps = {
  size?: "default" | "large";
};

export function BrandMark({ size = "default" }: BrandMarkProps) {
  const className = size === "large" ? "brand-mark brand-mark--large" : "brand-mark";
  return (
    <svg aria-hidden="true" className={className} focusable="false" viewBox="0 0 64 64">
      <rect className="brand-mark__field" height="64" rx="14" width="64" />
      <rect className="brand-mark__base" height="12" rx="6" width="32" x="16" y="37.5" />
      <rect
        className="brand-mark__middle"
        height="10"
        rx="5"
        transform="rotate(-5 32 31)"
        width="23"
        x="20.5"
        y="26"
      />
      <rect
        className="brand-mark__top"
        height="9.5"
        rx="4.75"
        transform="rotate(4 32 19.75)"
        width="14"
        x="25"
        y="15"
      />
    </svg>
  );
}
