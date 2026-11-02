/** The countries a place names or suggests, as one compact token; nothing when none. */
export function CountryToken({ countries }: { countries: readonly string[] | undefined }) {
  if (!countries?.length) return null;
  const names = countries.join(", ");
  return (
    <span className="country-token" title={`Countries this place names or suggests: ${names}`}>
      {names}
    </span>
  );
}
