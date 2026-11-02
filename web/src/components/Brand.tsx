type BrandMarkProps = {
  size?: "default" | "large";
};

export function BrandMark({ size = "default" }: BrandMarkProps) {
  const className = size === "large" ? "brand-mark brand-mark--large" : "brand-mark";
  return (
    <svg aria-hidden="true" className={className} focusable="false" viewBox="0 0 64 64">
      <rect className="brand-mark__field" height="64" rx="14" width="64" />
      <path
        className="brand-mark__arc"
        d="M17 36.5c-3.9-15.1 10.9-27.8 24.2-18.8 14.7 9.8 8.2 31.8-8.8 31.4-12.5-.3-20.5-13.4-14.2-24.2"
      />
      <path className="brand-mark__thread" d="M18 39.5c12.1-14.5 20.1 12.5 32-4.2" />
      <circle className="brand-mark__start" cx="18" cy="39.5" r="3.2" />
      <circle className="brand-mark__end" cx="50" cy="35.3" r="3.2" />
      <path className="brand-mark__letter" d="M23.5 23.4h17M32 24.5v24" />
    </svg>
  );
}
