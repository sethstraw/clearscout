/* eslint-disable react-refresh/only-export-components -- icon registry is intentionally static */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Icon({ children, ...props }: IconProps) {
  return (
    <svg aria-hidden="true" fill="none" height="20" viewBox="0 0 24 24" width="20" {...props}>
      {children}
    </svg>
  );
}

export const Icons = {
  home: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="M3 11.5 12 4l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1v-8.5Z"
        stroke="currentColor"
        strokeWidth="1.7"
      />
    </Icon>
  ),
  jobs: (props: IconProps) => (
    <Icon {...props}>
      <rect x="3" y="7" width="18" height="13" rx="2" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12h18"
        stroke="currentColor"
        strokeWidth="1.7"
      />
    </Icon>
  ),
  applications: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </Icon>
  ),
  cv: (props: IconProps) => (
    <Icon {...props}>
      <rect x="4" y="3" width="16" height="18" rx="2" stroke="currentColor" strokeWidth="1.7" />
      <circle cx="12" cy="9" r="2.3" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M8.2 16.6a3.9 3.9 0 0 1 7.6 0"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </Icon>
  ),
  documents: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <path d="M14 3v5h5" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
    </Icon>
  ),
  notes: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="M4 12.6V5a1 1 0 0 1 1-1h7.6a1 1 0 0 1 .7.3l6.4 6.4a1 1 0 0 1 0 1.4l-7.6 7.6a1 1 0 0 1-1.4 0l-6.4-6.4a1 1 0 0 1-.3-.7Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <circle cx="8.6" cy="8.6" r="1.4" stroke="currentColor" strokeWidth="1.7" />
    </Icon>
  ),
  prompts: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H12l-4.5 4v-4H5.5A1.5 1.5 0 0 1 4 14.5Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <path
        d="m8.5 8 2.2 2.2L8.5 12.4M13 12.4h3"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Icon>
  ),
  search: (props: IconProps) => (
    <Icon {...props}>
      <circle cx="11" cy="11" r="6" stroke="currentColor" strokeWidth="1.7" />
      <path d="m15.5 15.5 4 4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </Icon>
  ),
  settings: (props: IconProps) => (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M19 13.5v-3l-2-.6-.6-1.4 1-1.9-2.1-2.1-1.9 1-1.4-.6-.6-2h-3l-.6 2-1.4.6-1.9-1-2.1 2.1 1 1.9-.6 1.4-2 .6v3l2 .6.6 1.4-1 1.9 2.1 2.1 1.9-1 1.4.6.6 2h3l.6-2 1.4-.6 1.9 1 2.1-2.1-1-1.9.6-1.4 2-.6Z"
        stroke="currentColor"
        strokeWidth="1.3"
      />
    </Icon>
  ),
  tasks: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="M4 6h2M4 12h2M4 18h2"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
      <path
        d="M9 6h11M9 12h11M9 18h11"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </Icon>
  ),
  sun: (props: IconProps) => (
    <Icon {...props}>
      <circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </Icon>
  ),
  moon: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="M20 15.2A8.5 8.5 0 0 1 8.8 4 8.5 8.5 0 1 0 20 15.2Z"
        stroke="currentColor"
        strokeWidth="1.7"
      />
    </Icon>
  ),
  chevronLeft: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="m14 6-6 6 6 6"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Icon>
  ),
  chevronRight: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="m10 6 6 6-6 6"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Icon>
  ),
  sortAscending: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="m6 14 6-6 6 6"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Icon>
  ),
  sortDescending: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="m6 10 6 6 6-6"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Icon>
  ),
  sortUnsorted: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="m6 10 6-5 6 5M6 14l6 5 6-5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Icon>
  ),
  check: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="m5 12.5 4.5 4.5L19 7"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Icon>
  ),
  circle: (props: IconProps) => (
    <Icon {...props}>
      <circle cx="12" cy="12" r="7" stroke="currentColor" strokeWidth="1.7" />
    </Icon>
  ),
  close: (props: IconProps) => (
    <Icon {...props}>
      <path
        d="M7 7 17 17M17 7 7 17"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </Icon>
  ),
};
