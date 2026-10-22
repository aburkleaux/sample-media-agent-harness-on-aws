import IconBase, { type IconProps } from './IconBase';

/** The Assets view toggle's "list" segment: rows, each with a leading dot. */
export default function ListViewIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M9 6h11" />
      <path d="M9 12h11" />
      <path d="M9 18h11" />
      <path d="M4.5 6h.01" />
      <path d="M4.5 12h.01" />
      <path d="M4.5 18h.01" />
    </IconBase>
  );
}
