import IconBase, { type IconProps } from './IconBase';

/** Find similar: a magnifier. */
export default function SearchIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M20 20l-4.2-4.2" />
    </IconBase>
  );
}
