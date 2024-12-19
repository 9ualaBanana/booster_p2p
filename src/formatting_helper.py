from decimal import Decimal, ROUND_HALF_EVEN

class FormattingHelper:
    @staticmethod
    def quantize(value: Decimal, exp: int) -> Decimal:
        """
        Quantize a Decimal value to specified number of decimal places.
        
        Args:
            value (Decimal): The decimal value to quantize
            exp (int): Number of decimal places (can be negative)
            
        Returns:
            Decimal: Quantized decimal value
            
        Examples:
            >>> helper.quantize(Decimal('3.14159'), 2)
            Decimal('3.14')
            >>> helper.quantize(Decimal('3.14159'), -1)
            Decimal('3.0')
        """
        exponent = Decimal(10) ** -exp
        return str(value.quantize(exponent, rounding=ROUND_HALF_EVEN)).rstrip('0').rstrip('.')
