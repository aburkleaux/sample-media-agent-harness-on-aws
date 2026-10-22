import IconBase, { type IconProps } from './IconBase';

/** A collapsed disclosure: the section opens to the right/below when activated. */
export default function ChevronRightIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M9.5 6.5L15 12l-5.5 5.5" />
    </IconBase>
  );
}
