import IconBase, { type IconProps } from './IconBase';

/** Opens a popover or dropdown below/above its control. */
export default function ChevronDownIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M6.5 9.5L12 15l5.5-5.5" />
    </IconBase>
  );
}
