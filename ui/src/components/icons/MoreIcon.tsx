import IconBase, { type IconProps } from './IconBase';

/** Overflow menu: three horizontal dots. */
export default function MoreIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="5.5" cy="12" r="1.1" />
      <circle cx="12" cy="12" r="1.1" />
      <circle cx="18.5" cy="12" r="1.1" />
    </IconBase>
  );
}
