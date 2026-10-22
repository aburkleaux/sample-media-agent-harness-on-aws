import IconBase, { type IconProps } from './IconBase';

/** Audio asset: a pair of eighth notes. */
export default function AudioIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M9 18V6l11-2v12" />
      <circle cx="6.5" cy="18" r="2.5" />
      <circle cx="17.5" cy="16" r="2.5" />
    </IconBase>
  );
}
